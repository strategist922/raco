import copy
import itertools
from collections import defaultdict, deque
from operator import mul, add
from abc import abstractmethod

from raco import algebra, expression, rules
from raco.catalog import MyriaCatalog
from raco.language import Language
from raco.utility import emit
from raco.relation_key import RelationKey
from expression import (accessed_columns, to_unnamed_recursive,
                        UnnamedAttributeRef)
from raco.expression.aggregate import DecomposableAggregate
from raco.datastructure.UnionFind import UnionFind
from raco import types


def scheme_to_schema(s):
    if s:
        names, descrs = zip(*s.asdict.items())
        names = ["%s" % n for n in names]
        types = [r[1] for r in descrs]
    else:
        names = []
        types = []
    return {"columnTypes": types, "columnNames": names}


def compile_expr(op, child_scheme, state_scheme):
    ####
    # Put special handling at the top!
    ####
    if isinstance(op, expression.NumericLiteral):
        if type(op.value) == int:
            if (2 ** 31) - 1 >= op.value >= -2 ** 31:
                myria_type = 'INT_TYPE'
            else:
                myria_type = types.LONG_TYPE
        elif type(op.value) == float:
            myria_type = types.DOUBLE_TYPE
        else:
            raise NotImplementedError("Compiling NumericLiteral %s of type %s" % (op, type(op.value)))  # noqa

        return {
            'type': 'CONSTANT',
            'value': str(op.value),
            'valueType': myria_type
        }
    elif isinstance(op, expression.StringLiteral):
        return {
            'type': 'CONSTANT',
            'value': str(op.value),
            'valueType': 'STRING_TYPE'
        }
    elif isinstance(op, expression.StateRef):
        return {
            'type': 'STATE',
            'columnIdx': op.get_position(child_scheme, state_scheme)
        }
    elif isinstance(op, expression.AttributeRef):
        return {
            'type': 'VARIABLE',
            'columnIdx': op.get_position(child_scheme, state_scheme)
        }
    elif isinstance(op, expression.Case):
        # Convert n-ary case statements to binary, as expected by Myria
        op = op.to_binary()
        assert len(op.when_tuples) == 1

        if_expr = compile_expr(op.when_tuples[0][0], child_scheme,
                               state_scheme)
        then_expr = compile_expr(op.when_tuples[0][1], child_scheme,
                                 state_scheme)
        else_expr = compile_expr(op.else_expr, child_scheme, state_scheme)

        return {
            'type': 'CONDITION',
            'children': [if_expr, then_expr, else_expr]
        }
    elif isinstance(op, expression.CAST):
        return {
            'type': 'CAST',
            'left': compile_expr(op.input, child_scheme, state_scheme),
            'right': {
                'type': 'TYPE',
                'outputType': op._type
            }
        }

    ####
    # Everything below here is compiled automatically
    ####
    elif isinstance(op, expression.UnaryOperator):
        return {
            'type': op.opname(),
            'operand': compile_expr(op.input, child_scheme, state_scheme)
        }
    elif isinstance(op, expression.BinaryOperator):
        return {
            'type': op.opname(),
            'left': compile_expr(op.left, child_scheme, state_scheme),
            'right': compile_expr(op.right, child_scheme, state_scheme)
        }
    elif isinstance(op, expression.ZeroaryOperator):
        return {
            'type': op.opname(),
        }
    elif isinstance(op, expression.NaryOperator):
        children = []
        for operand in op.operands:
            children.append(compile_expr(operand, child_scheme, state_scheme))
        return {
            'type': op.opname(),
            'children': children
        }
    raise NotImplementedError("Compiling expr of class %s" % op.__class__)


def compile_mapping(expr, child_scheme, state_scheme):
    output_name, root_op = expr
    return {
        'outputName': output_name,
        'rootExpressionOperator': compile_expr(root_op,
                                               child_scheme,
                                               state_scheme)
    }


class MyriaLanguage(Language):
    reusescans = False

    @classmethod
    def new_relation_assignment(cls, rvar, val):
        return emit(cls.relation_decl(rvar), cls.assignment(rvar, val))

    @classmethod
    def relation_decl(cls, rvar):
        # no type declarations necessary
        return ""

    @staticmethod
    def assignment(x, y):
        return ""

    @staticmethod
    def comment(txt):
        # comments not technically allowed in json
        return ""

    @classmethod
    def boolean_combine(cls, args, operator="and"):
        opstr = " %s " % operator
        conjunc = opstr.join(["%s" % arg for arg in args])
        return "(%s)" % conjunc

    @staticmethod
    def mklambda(body, var="t"):
        return ("lambda %s: " % var) + body

    @staticmethod
    def compile_attribute(name):
        return '%s' % name


class MyriaOperator(object):
    language = MyriaLanguage


def relation_key_to_json(relation_key):
    return {"userName": relation_key.user,
            "programName": relation_key.program,
            "relationName": relation_key.relation}


class MyriaScan(algebra.Scan, MyriaOperator):
    def compileme(self):
        return {
            "opType": "TableScan",
            "relationKey": relation_key_to_json(self.relation_key),
            "temporary": False,
        }


class MyriaScanTemp(algebra.ScanTemp, MyriaOperator):
    def compileme(self):
        return {
            "opType": "TableScan",
            "relationKey": relation_key_to_json(RelationKey.from_string(
                "public:__TEMP__:" + self.name)),
            "temporary": True,
        }


class MyriaUnionAll(algebra.UnionAll, MyriaOperator):
    def compileme(self, leftid, rightid):
        return {
            "opType": "UnionAll",
            "argChildren": [leftid, rightid]
        }


class MyriaDifference(algebra.Difference, MyriaOperator):
    def compileme(self, leftid, rightid):
        return {
            "opType": "Difference",
            "argChild1": leftid,
            "argChild2": rightid,
        }


class MyriaSingleton(algebra.SingletonRelation, MyriaOperator):
    def compileme(self):
        return {
            "opType": "Singleton",
        }


class MyriaEmptyRelation(algebra.EmptyRelation, MyriaOperator):
    def compileme(self):
        return {
            "opType": "Empty",
            'schema': scheme_to_schema(self.scheme())
        }


class MyriaSelect(algebra.Select, MyriaOperator):
    def compileme(self, inputid):
        pred = compile_expr(self.condition, self.scheme(), None)
        return {
            "opType": "Filter",
            "argChild": inputid,
            "argPredicate": {
                "rootExpressionOperator": pred
            }
        }


class MyriaCrossProduct(algebra.CrossProduct, MyriaOperator):
    def compileme(self, leftid, rightid):
        column_names = [name for (name, _) in self.scheme()]
        allleft = [i.position for i in self.left.scheme().ascolumnlist()]
        allright = [i.position for i in self.right.scheme().ascolumnlist()]
        return {
            "opType": "SymmetricHashJoin",
            "argColumnNames": column_names,
            "argChild1": leftid,
            "argChild2": rightid,
            "argColumns1": [],
            "argColumns2": [],
            "argSelect1": allleft,
            "argSelect2": allright
        }


class MyriaStore(algebra.Store, MyriaOperator):
    def compileme(self, inputid):
        return {
            "opType": "DbInsert",
            "relationKey": relation_key_to_json(self.relation_key),
            "argOverwriteTable": True,
            "argTemporary": False,
            "argChild": inputid,
        }


class MyriaStoreTemp(algebra.StoreTemp, MyriaOperator):
    def compileme(self, inputid):
        return {
            "opType": "DbInsert",
            "relationKey": relation_key_to_json(RelationKey.from_string(
                "public:__TEMP__:" + self.name)),
            "argTemporary": True,
            "argOverwriteTable": True,
            "argChild": inputid,
        }


def convertcondition(condition, left_len, combined_scheme):
    """Convert an equijoin condition to a pair of column lists."""

    if isinstance(condition, expression.AND):
        leftcols1, rightcols1 = convertcondition(condition.left,
                                                 left_len,
                                                 combined_scheme)
        leftcols2, rightcols2 = convertcondition(condition.right,
                                                 left_len,
                                                 combined_scheme)
        return leftcols1 + leftcols2, rightcols1 + rightcols2

    if isinstance(condition, expression.EQ):
        leftpos = condition.left.get_position(combined_scheme)
        rightpos = condition.right.get_position(combined_scheme)
        leftcol = min(leftpos, rightpos)
        rightcol = max(leftpos, rightpos)
        assert rightcol >= left_len
        return [leftcol], [rightcol - left_len]

    raise NotImplementedError("Myria only supports EquiJoins, not %s" % condition)  # noqa


def convert_nary_conditions(conditions, schemes):
    """Convert an nary join map from global column index to local"""
    attr_map = {}   # map of global attribute to local column index
    count = 0
    for i, scheme in enumerate(schemes):
        for j, attr in enumerate(scheme.ascolumnlist()):
            attr_map[count] = [i, j]
            count += 1
    new_conditions = []   # arrays of [child_index, column_index]
    for join_cond in conditions:
        new_join_cond = []
        for attr in join_cond:
            new_join_cond.append(attr_map[attr.position])
        new_conditions.append(new_join_cond)
    return new_conditions


class MyriaSymmetricHashJoin(algebra.ProjectingJoin, MyriaOperator):
    def compileme(self, leftid, rightid):
        """Compile the operator to a sequence of json operators"""

        left_len = len(self.left.scheme())
        combined = self.left.scheme() + self.right.scheme()
        leftcols, rightcols = convertcondition(self.condition,
                                               left_len,
                                               combined)

        if self.output_columns is None:
            self.output_columns = self.scheme().ascolumnlist()
        column_names = [name for (name, _) in self.scheme()]
        pos = [i.get_position(combined) for i in self.output_columns]
        allleft = [i for i in pos if i < left_len]
        allright = [i - left_len for i in pos if i >= left_len]

        join = {
            "opType": "SymmetricHashJoin",
            "argColumnNames": column_names,
            "argChild1": "%s" % leftid,
            "argColumns1": leftcols,
            "argChild2": "%s" % rightid,
            "argColumns2": rightcols,
            "argSelect1": allleft,
            "argSelect2": allright
        }

        return join


class MyriaLeapFrogJoin(algebra.NaryJoin, MyriaOperator):

    def compileme(self, *args):
        def convert_join_cond(pos_to_rel_col, cond, scheme):
            join_col_pos = [c.get_position(scheme) for c in cond]
            return [pos_to_rel_col[p] for p in join_col_pos]
        # map a output column to its origin
        rel_of_pos = {}     # pos => [rel_idx, field_idx]
        schemes = [c.scheme().ascolumnlist() for c in self.children()]
        pos = 0
        combined = []
        for rel_idx, scheme in enumerate(schemes):
            combined.extend(scheme)
            for field_idx in xrange(len(scheme)):
                rel_of_pos[pos] = [rel_idx, field_idx]
                pos += 1
        # build column names
        if self.output_columns is None:
            self.output_columns = self.scheme().ascolumnlist()
        column_names = [name for (name, _) in self.scheme()]
        # get rel_idx and field_idx of select columns
        out_pos_list = [
            i.get_position(combined) for i in list(self.output_columns)]
        output_fields = [rel_of_pos[p] for p in out_pos_list]
        join_fields = [
            convert_join_cond(rel_of_pos, cond, combined)
            for cond in self.conditions]
        return {
            "opType": "LeapFrogJoin",
            "joinFieldMapping": join_fields,
            "argColumnNames": column_names,
            "outputFieldMapping": output_fields,
            "argChildren": args
        }


class MyriaGroupBy(algebra.GroupBy, MyriaOperator):
    @staticmethod
    def agg_mapping(agg_expr):
        """Maps an AggregateExpression to a Myria string constant representing
        the corresponding aggregate operation."""
        if isinstance(agg_expr, expression.MAX):
            return "AGG_OP_MAX"
        elif isinstance(agg_expr, expression.MIN):
            return "AGG_OP_MIN"
        elif isinstance(agg_expr, expression.COUNT):
            return "AGG_OP_COUNT"
        elif isinstance(agg_expr, expression.COUNTALL):
            return "AGG_OP_COUNT"  # XXX Wrong in the presence of nulls
        elif isinstance(agg_expr, expression.SUM):
            return "AGG_OP_SUM"

    def compileme(self, inputid):
        child_scheme = self.input.scheme()
        group_fields = [expression.toUnnamed(ref, child_scheme)
                        for ref in self.grouping_list]

        agg_fields = []
        for expr in self.aggregate_list:
            if isinstance(expr, expression.COUNTALL):
                # XXX Wrong in the presence of nulls
                agg_fields.append(UnnamedAttributeRef(0))
            else:
                agg_fields.append(expression.toUnnamed(
                    expr.input, child_scheme))

        agg_types = [[MyriaGroupBy.agg_mapping(agg_expr)]
                     for agg_expr in self.aggregate_list]
        ret = {
            "argChild": inputid,
            "argAggFields": [agg_field.position for agg_field in agg_fields],
            "argAggOperators": agg_types,
        }

        num_fields = len(self.grouping_list)
        if num_fields == 0:
            ret["opType"] = "Aggregate"
        elif num_fields == 1:
            ret["opType"] = "SingleGroupByAggregate"
            ret["argGroupField"] = group_fields[0].position
        else:
            ret["opType"] = "MultiGroupByAggregate"
            ret["argGroupFields"] = [field.position for field in group_fields]
        return ret


class MyriaInMemoryOrderBy(algebra.OrderBy, MyriaOperator):

    def compileme(self, inputsym):
        return {
            "opType": "InMemoryOrderBy",
            "argChild": inputsym,
            "argSortColumns": self.sort_columns,
            "argAscending": self.ascending
        }


class MyriaShuffle(algebra.Shuffle, MyriaOperator):
    """Represents a simple shuffle operator"""

    def compileme(self, inputid):
        raise NotImplementedError('shouldn''t ever get here, should be turned into SP-SC pair')  # noqa


class MyriaCollect(algebra.Collect, MyriaOperator):
    """Represents a simple collect operator"""

    def compileme(self, inputid):
        raise NotImplementedError('shouldn''t ever get here, should be turned into CP-CC pair')  # noqa


class MyriaDupElim(algebra.Distinct, MyriaOperator):
    """Represents duplicate elimination"""

    def compileme(self, inputid):
        return {
            "opType": "DupElim",
            "argChild": inputid,
        }


class MyriaApply(algebra.Apply, MyriaOperator):
    """Represents a simple apply operator"""

    def compileme(self, inputid):
        child_scheme = self.input.scheme()
        emitters = [compile_mapping(x, child_scheme, None)
                    for x in self.emitters]
        return {
            'opType': 'Apply',
            'argChild': inputid,
            'emitExpressions': emitters
        }


class MyriaStatefulApply(algebra.StatefulApply, MyriaOperator):
    """Represents a stateful apply operator"""

    def compileme(self, inputid):
        child_scheme = self.input.scheme()
        state_scheme = self.state_scheme
        comp_map = lambda x: compile_mapping(x, child_scheme, state_scheme)
        emitters = [comp_map(x) for x in self.emitters]
        inits = [comp_map(x) for x in self.inits]
        updaters = [comp_map(x) for x in self.updaters]
        return {
            'opType': 'StatefulApply',
            'argChild': inputid,
            'emitExpressions': emitters,
            'initializerExpressions': inits,
            'updaterExpressions': updaters
        }


class MyriaBroadcastProducer(algebra.UnaryOperator, MyriaOperator):
    """A Myria BroadcastProducer"""

    def __init__(self, input):
        algebra.UnaryOperator.__init__(self, input)

    def num_tuples(self):
        return self.input.num_tuples()

    def shortStr(self):
        return "%s" % self.opname()

    def compileme(self, inputid):
        return {
            "opType": "BroadcastProducer",
            "argChild": inputid,
        }


class MyriaBroadcastConsumer(algebra.UnaryOperator, MyriaOperator):
    """A Myria BroadcastConsumer"""

    def __init__(self, input):
        algebra.UnaryOperator.__init__(self, input)

    def num_tuples(self):
        return self.input.num_tuples()

    def shortStr(self):
        return "%s" % self.opname()

    def compileme(self, inputid):
        return {
            'opType': 'BroadcastConsumer',
            'argOperatorId': inputid
        }


class MyriaShuffleProducer(algebra.UnaryOperator, MyriaOperator):
    """A Myria ShuffleProducer"""

    def __init__(self, input, hash_columns):
        algebra.UnaryOperator.__init__(self, input)
        self.hash_columns = hash_columns

    def shortStr(self):
        hash_string = ','.join([str(x) for x in self.hash_columns])
        return "%s(h(%s))" % (self.opname(), hash_string)

    def num_tuples(self):
        return self.input.num_tuples()

    def compileme(self, inputid):
        if len(self.hash_columns) == 1:
            pf = {
                "type": "SingleFieldHash",
                "index": self.hash_columns[0].position
            }
        else:
            pf = {
                "type": "MultiFieldHash",
                "indexes": [x.position for x in self.hash_columns]
            }

        return {
            "opType": "ShuffleProducer",
            "argChild": inputid,
            "argPf": pf
        }


class MyriaShuffleConsumer(algebra.UnaryOperator, MyriaOperator):
    """A Myria ShuffleConsumer"""

    def __init__(self, input):
        algebra.UnaryOperator.__init__(self, input)

    def num_tuples(self):
        return self.input.num_tuples()

    def shortStr(self):
        return "%s" % self.opname()

    def compileme(self, inputid):
        return {
            'opType': 'ShuffleConsumer',
            'argOperatorId': inputid
        }


class MyriaCollectProducer(algebra.UnaryOperator, MyriaOperator):
    """A Myria CollectProducer"""

    def __init__(self, input, server):
        algebra.UnaryOperator.__init__(self, input)
        self.server = server

    def num_tuples(self):
        return self.input.num_tuples()

    def shortStr(self):
        return "%s(@%s)" % (self.opname(), self.server)

    def compileme(self, inputid):
        return {
            "opType": "CollectProducer",
            "argChild": inputid,
        }


class MyriaCollectConsumer(algebra.UnaryOperator, MyriaOperator):
    """A Myria CollectConsumer"""

    def __init__(self, input):
        algebra.UnaryOperator.__init__(self, input)

    def num_tuples(self):
        return self.input.num_tuples()

    def shortStr(self):
        return "%s" % self.opname()

    def compileme(self, inputid):
        return {
            'opType': 'CollectConsumer',
            'argOperatorId': inputid
        }


class MyriaHyperShuffle(algebra.HyperCubeShuffle, MyriaOperator):
    """Represents a HyperShuffle shuffle operator"""
    def compileme(self, inputsym):
        raise NotImplementedError('shouldn''t ever get here, should be turned into SP-SC pair')  # noqa


class MyriaHyperShuffleProducer(algebra.UnaryOperator, MyriaOperator):
    """A Myria HyperShuffleProducer"""
    def __init__(self, input, hashed_columns,
                 hyper_cube_dims, mapped_hc_dims, cell_partition):
        algebra.UnaryOperator.__init__(self, input)
        self.hashed_columns = hashed_columns
        self.mapped_hc_dimensions = mapped_hc_dims
        self.hyper_cube_dimensions = hyper_cube_dims
        self.cell_partition = cell_partition

    def num_tuples(self):
        return self.input.num_tuples()

    def shortStr(self):
        hash_string = ','.join([str(x) for x in self.hashed_columns])
        return "%s(h(%s))" % (self.opname(), hash_string)

    def compileme(self, inputsym):
        return {
            "opType": "HyperShuffleProducer",
            "hashedColumns": list(self.hashed_columns),
            "mappedHCDimensions": list(self.mapped_hc_dimensions),
            "hyperCubeDimensions": list(self.hyper_cube_dimensions),
            "cellPartition": self.cell_partition,
            "argChild": inputsym
        }


class MyriaHyperShuffleConsumer(algebra.UnaryOperator, MyriaOperator):
    """A Myria HyperShuffleConsumer"""
    def __init__(self, input):
        algebra.UnaryOperator.__init__(self, input)

    def num_tuples(self):
        return self.input.num_tuples()

    def shortStr(self):
        return "%s" % self.opname()

    def compileme(self, inputsym):
        return {
            "opType": "HyperShuffleConsumer",
            "argOperatorId": inputsym
        }


class BreakShuffle(rules.Rule):
    def fire(self, expr):
        if not isinstance(expr, MyriaShuffle):
            return expr

        producer = MyriaShuffleProducer(expr.input, expr.columnlist)
        consumer = MyriaShuffleConsumer(producer)
        return consumer


class BreakHyperCubeShuffle(rules.Rule):
    def fire(self, expr):
        """
        self.hashed_columns = hashed_columns
        self.mapped_hc_dimensions = mapped_hc_dims
        self.hyper_cube_dimensions = hyper_cube_dims
        self.cell_partition = cell_partition
        """
        if not isinstance(expr, MyriaHyperShuffle):
            return expr
        producer = MyriaHyperShuffleProducer(
            expr.input, expr.hashed_columns, expr.hyper_cube_dimensions,
            expr.mapped_hc_dimensions, expr.cell_partition)
        consumer = MyriaHyperShuffleConsumer(producer)
        return consumer


class BreakCollect(rules.Rule):
    def fire(self, expr):
        if not isinstance(expr, MyriaCollect):
            return expr

        producer = MyriaCollectProducer(expr.input, None)
        consumer = MyriaCollectConsumer(producer)
        return consumer


class BreakBroadcast(rules.Rule):
    def fire(self, expr):
        if not isinstance(expr, algebra.Broadcast):
            return expr

        producer = MyriaBroadcastProducer(expr.input)
        consumer = MyriaBroadcastConsumer(producer)
        return consumer


class ShuffleBeforeDistinct(rules.Rule):
    def fire(self, exp):
        if not isinstance(exp, algebra.Distinct):
            return exp
        if isinstance(exp.input, algebra.Shuffle):
            return exp
        cols = [expression.UnnamedAttributeRef(i)
                for i in range(len(exp.scheme()))]
        exp.input = algebra.Shuffle(child=exp.input, columnlist=cols)
        return exp


def check_shuffle_xor(exp):
    """Enforce that neither or both inputs to a binary op are shuffled.

    Return True if the arguments are shuffled; False if they are not;
    or raise a ValueError on xor failure.

    Note that we assume that inputs are shuffled in a compatible way.
    """
    left_shuffle = isinstance(exp.left, algebra.Shuffle)
    right_shuffle = isinstance(exp.right, algebra.Shuffle)

    if left_shuffle and right_shuffle:
        return True
    if left_shuffle or right_shuffle:
        raise ValueError("Must shuffle on both inputs of %s" % exp)
    return False


class ShuffleBeforeSetop(rules.Rule):
    def fire(self, exp):
        if not isinstance(exp, (algebra.Difference, algebra.Intersection)):
            return exp

        def shuffle_after(op):
            cols = [expression.UnnamedAttributeRef(i)
                    for i in range(len(op.scheme()))]
            return algebra.Shuffle(child=op, columnlist=cols)

        if not check_shuffle_xor(exp):
            exp.left = shuffle_after(exp.left)
            exp.right = shuffle_after(exp.right)
        return exp


class ShuffleBeforeJoin(rules.Rule):
    def fire(self, expr):
        # If not a join, who cares?
        if not isinstance(expr, algebra.Join):
            return expr

        # If both have shuffles already, who cares?
        if check_shuffle_xor(expr):
            return expr

        # Figure out which columns go in the shuffle
        left_cols, right_cols = \
            convertcondition(expr.condition,
                             len(expr.left.scheme()),
                             expr.left.scheme() + expr.right.scheme())

        # Left shuffle
        if isinstance(expr.left, algebra.Shuffle):
            left_shuffle = expr.left
        else:
            left_cols = [expression.UnnamedAttributeRef(i)
                         for i in left_cols]
            left_shuffle = algebra.Shuffle(expr.left, left_cols)
        # Right shuffle
        if isinstance(expr.right, algebra.Shuffle):
            right_shuffle = expr.right
        else:
            right_cols = [expression.UnnamedAttributeRef(i)
                          for i in right_cols]
            right_shuffle = algebra.Shuffle(expr.right, right_cols)

        # Construct the object!
        if isinstance(expr, algebra.ProjectingJoin):
            return algebra.ProjectingJoin(expr.condition,
                                          left_shuffle, right_shuffle,
                                          expr.output_columns)
        elif isinstance(expr, algebra.Join):
            return algebra.Join(expr.condition, left_shuffle, right_shuffle)
        raise NotImplementedError("How the heck did you get here?")


class HCShuffleBeforeNaryJoin(rules.Rule):
    def __init__(self, catalog):
        assert isinstance(catalog, MyriaCatalog)
        self.catalog = catalog

    @staticmethod
    def reversed_index(child_schemes, conditions):
        """Return the reversed index of join conditions. The reverse index
           specify for each column on each relation, which hypercube dimension
           it is mapped to, -1 means this columns is not in the hyper cube
           (not joined).

        Keyword arguments:
        child_schemes -- schemes of children.
        conditions -- join conditions.
        """
        # make it -1 first
        r_index = [[-1] * len(scheme) for scheme in child_schemes]
        for i, jf_list in enumerate(conditions):
            for jf in jf_list:
                r_index[jf[0]][jf[1]] = i
        return r_index

    @staticmethod
    def workload(dim_sizes, child_sizes, r_index):
        """Compute the workload given a hyper cube size assignment"""
        load = 0.0
        for i, size in enumerate(child_sizes):
            # compute subcube sizes
            scale = 1
            for index in r_index[i]:
                if index != -1:
                    scale = scale * dim_sizes[index]
            # add load per server by child i
            load += float(child_sizes[i]) / float(scale)
        return load

    @staticmethod
    def get_hyper_cube_dim_size(num_server, child_sizes,
                                conditions, r_index):
        """Find the optimal hyper cube dimension sizes using BFS.

        Keyword arguments:
        num_server -- number of servers, this sets upper bound of HC cells.
        child_sizes -- cardinality of each child.
        child_schemes -- schemes of children.
        conditions -- join conditions.
        r_index -- reversed index of join conditions.
        """
        # Helper function: compute the product.
        def product(array):
            return reduce(mul, array, 1)
        # Use BFS to find the best possible assignment.
        this = HCShuffleBeforeNaryJoin
        visited = set()
        toVisit = deque()
        toVisit.append(tuple([1 for _ in conditions]))
        min_work_load = sum(child_sizes)
        while len(toVisit) > 0:
            dim_sizes = toVisit.pop()
            if this.workload(dim_sizes, child_sizes, r_index) < min_work_load:
                min_work_load = this.workload(
                    dim_sizes, child_sizes, r_index)
                opt_dim_sizes = dim_sizes
            visited.add(dim_sizes)
            for i, d in enumerate(dim_sizes):
                new_dim_sizes = (dim_sizes[0:i] +
                                 tuple([dim_sizes[i] + 1]) +
                                 dim_sizes[i + 1:])
                if (product(new_dim_sizes) <= num_server
                        and new_dim_sizes not in visited):
                    toVisit.append(new_dim_sizes)
        return opt_dim_sizes, min_work_load

    @staticmethod
    def coord_to_worker_id(coordinate, dim_sizes):
        """Convert coordinate of cell to worker id

        Keyword arguments:
        coordinate -- coordinate of hyper cube cell.
        dim_sizes -- sizes of dimensons of hyper cube.
        """
        assert len(coordinate) == len(dim_sizes)
        ret = 0
        for k, v in enumerate(coordinate):
            ret += v * reduce(mul, dim_sizes[k + 1:], 1)
        return ret

    @staticmethod
    def get_cell_partition(dim_sizes, conditions,
                           child_schemes, child_idx, hashed_columns):
        """Generate the cell_partition for a specific child.

        Keyword arguments:
        dim_sizes -- size of each dimension of the hypercube.
        conditions -- each element is an array of (child_idx, column).
        child_schemes -- schemes of children.
        child_idx -- index of this child.
        hashed_columns -- hashed columns of this child.
        """
        assert len(dim_sizes) == len(conditions)
        # make life a little bit easier
        this = HCShuffleBeforeNaryJoin
        # get reverse index
        r_index = this.reversed_index(child_schemes, conditions)
        # find which dims in hyper cube this relation is involved
        hashed_dims = [r_index[child_idx][col] for col in hashed_columns]
        assert -1 not in hashed_dims
        # group by cell according to their projection on subcube voxel
        cell_partition = defaultdict(list)
        coor_ranges = [list(range(d)) for d in dim_sizes]
        for coordinate in itertools.product(*coor_ranges):
            # project a hypercube cell to a subcube voxel
            voxel = [coordinate[dim] for dim in hashed_dims]
            cell_partition[tuple(voxel)].append(
                this.coord_to_worker_id(coordinate, dim_sizes))
        return [wid for vox, wid in sorted(cell_partition.items())]

    def fire(self, expr):
        def add_hyper_shuffle():
            """ Helper function: put a HyperCube shuffle before each child."""
            # make calling static method easier
            this = HCShuffleBeforeNaryJoin
            # get child schemes
            child_schemes = [op.scheme() for op in expr.children()]
            # convert join conditions from expressions to 2d array
            conditions = convert_nary_conditions(
                expr.conditions, child_schemes)
            # get number of servers from catalog
            num_server = self.catalog.get_num_servers()
            # get estimated cardinalities of children
            child_sizes = [child.num_tuples() for child in expr.children()]
            # get reversed index of join conditions
            r_index = this.reversed_index(child_schemes, conditions)
            # compute optimal dimension sizes
            (dim_sizes, workload) = this.get_hyper_cube_dim_size(
                num_server, child_sizes, conditions, r_index)
            # specify HyperCube shuffle to each child
            new_children = []
            for child_idx, child in enumerate(expr.children()):
                # (mapped hc dimension, column index)
                hashed_fields = [(hc_dim, i)
                                 for i, hc_dim
                                 in enumerate(r_index[child_idx])
                                 if hc_dim != -1]
                mapped_dims, hashed_columns = zip(*sorted(hashed_fields))
                # get cell partition for child i
                cell_partition = this.get_cell_partition(
                    dim_sizes, conditions, child_schemes,
                    child_idx, hashed_columns)
                # generate new children
                new_children.append(
                    algebra.HyperCubeShuffle(
                        child, hashed_columns, mapped_dims,
                        dim_sizes, cell_partition))
            # replace the children
            expr.args = new_children

        # only apply to NaryJoin
        if not isinstance(expr, algebra.NaryJoin):
            return expr
        # check if HC shuffle has been placed before
        shuffled_child = [isinstance(op, algebra.HyperCubeShuffle)
                          for op in list(expr.children())]
        if all(shuffled_child):    # already shuffled
            assert len(expr.children()) > 0
            return expr
        elif any(shuffled_child):
            raise NotImplementedError("NaryJoin is partially shuffled?")
        else:                      # add shuffle and order by
            add_hyper_shuffle()
            return expr


class OrderByBeforeNaryJoin(rules.Rule):
    def fire(self, expr):
        # if not Nary join, who cares?
        if not isinstance(expr, algebra.NaryJoin):
            return expr
        ordered_child = sum(
            [1 for child in expr.children()
             if isinstance(child, algebra.OrderBy)])

        # already applied
        if ordered_child == len(expr.children()):
            return expr
        elif ordered_child > 0:
            raise Exception("children are partially ordered? ")

        new_children = []
        for child in expr.children():
            # check: this rule must be applied after shuffle
            assert isinstance(child, algebra.HyperCubeShuffle)
            ascending = [True] * len(child.hashed_columns)
            new_children.append(
                algebra.OrderBy(
                    child, child.hashed_columns, ascending))
        expr.args = new_children
        return expr


class BroadcastBeforeCross(rules.Rule):
    def fire(self, expr):
        # If not a CrossProduct, who cares?
        if not isinstance(expr, algebra.CrossProduct):
            return expr

        if (isinstance(expr.left, algebra.Broadcast) or
                isinstance(expr.right, algebra.Broadcast)):
            return expr

        # By default, broadcast the right child
        expr.right = algebra.Broadcast(expr.right)

        return expr


class DistributedGroupBy(rules.Rule):
    @staticmethod
    def do_transfer(op):
        """Introduce a network transfer before a groupby operation."""

        # Get an array of position references to columns in the child scheme
        child_scheme = op.input.scheme()
        group_fields = [expression.toUnnamed(ref, child_scheme)
                        for ref in op.grouping_list]
        if len(group_fields) == 0:
            # Need to Collect all tuples at once place
            op.input = algebra.Collect(op.input)
        else:
            # Need to Shuffle
            op.input = algebra.Shuffle(op.input, group_fields)

        return op

    def fire(self, op):
        # If not a GroupBy, who cares?
        if op.__class__ != algebra.GroupBy:
            return op

        num_grouping_terms = len(op.grouping_list)
        decomposable_aggs = [agg for agg in op.aggregate_list if
                             isinstance(agg, DecomposableAggregate)]

        # All built-in aggregates are now decomposable
        assert len(decomposable_aggs) == len(op.aggregate_list)

        # Each logical aggregate generates one or more local aggregates:
        # e.g., average requires a SUM and a COUNT.  In turn, these local
        # aggregates are consumed by merge aggregates.

        local_aggs = []  # aggregates executed on each local machine
        merge_aggs = []  # aggregates executed after local aggs
        agg_offsets = defaultdict(list)  # map aggregate to local agg indices

        for (i, logical_agg) in enumerate(op.aggregate_list):
            for local, merge in zip(logical_agg.get_local_aggregates(),
                                    logical_agg.get_merge_aggregates()):
                try:
                    idx = local_aggs.index(local)
                    agg_offsets[i].append(idx)
                except ValueError:
                    agg_offsets[i].append(len(local_aggs))
                    local_aggs.append(local)
                    merge_aggs.append(merge)

        assert len(merge_aggs) == len(local_aggs)

        local_gb = MyriaGroupBy(op.grouping_list, local_aggs, op.input)

        # Create a merge aggregate; grouping terms are passed through.
        merge_groupings = [UnnamedAttributeRef(i)
                           for i in range(num_grouping_terms)]

        # Connect the output of local aggregates to merge aggregates
        for pos, agg in enumerate(merge_aggs, num_grouping_terms):
            agg.input = UnnamedAttributeRef(pos)

        merge_gb = MyriaGroupBy(merge_groupings, merge_aggs, local_gb)
        op_out = self.do_transfer(merge_gb)

        # Extract a single result per logical aggregate using the finalizer
        # expressions (if any)
        has_finalizer = any([agg.get_finalizer() for agg in op.aggregate_list])
        if not has_finalizer:
            return op_out

        def resolve_finalizer_expr(logical_agg, pos):
            assert isinstance(logical_agg, DecomposableAggregate)
            fexpr = logical_agg.get_finalizer()

            # Start of merge aggregates for this logical aggregate
            offsets = [idx + num_grouping_terms for idx in agg_offsets[pos]]

            if fexpr is None:
                assert len(offsets) == 1
                return UnnamedAttributeRef(offsets[0])
            else:
                # Convert MergeAggregateOutput instances to absolute col refs
                return expression.finalizer_expr_to_absolute(fexpr, offsets)

        # pass through grouping terms
        gmappings = [(None, UnnamedAttributeRef(i))
                     for i in range(len(op.grouping_list))]
        # extract a single result for aggregate terms
        fmappings = [(None, resolve_finalizer_expr(agg, pos)) for pos, agg in
                     enumerate(op.aggregate_list)]
        return algebra.Apply(gmappings + fmappings, op_out)


class SplitSelects(rules.Rule):
    """Replace AND clauses with multiple consecutive selects."""

    def fire(self, op):
        if not isinstance(op, algebra.Select):
            return op

        conjuncs = expression.extract_conjuncs(op.condition)
        assert conjuncs  # Must be at least 1

        # Normalize named references to integer indexes
        scheme = op.scheme()
        conjuncs = [to_unnamed_recursive(c, scheme)
                    for c in conjuncs]

        op.condition = conjuncs[0]
        op.has_been_pushed = False
        for conjunc in conjuncs[1:]:
            op = algebra.Select(conjunc, op)
            op.has_been_pushed = False
        return op

    def __str__(self):
        return "Select => Select, Select"


class MergeSelects(rules.Rule):
    """Merge consecutive Selects into a single conjunctive selection."""

    def fire(self, op):
        if not isinstance(op, algebra.Select):
            return op

        while isinstance(op.input, algebra.Select):
            conjunc = expression.AND(op.condition, op.input.condition)
            op = algebra.Select(conjunc, op.input.input)

        return op

    def __str__(self):
        return "Select, Select => Select"


class ProjectToDistinctColumnSelect(rules.Rule):
    def fire(self, expr):
        # If not a Project, who cares?
        if not isinstance(expr, algebra.Project):
            return expr

        mappings = [(None, x) for x in expr.columnlist]
        colSelect = algebra.Apply(mappings, expr.input)
        # TODO(dhalperi) the distinct logic is broken because we don't have a
        # locality-aware optimizer. For now, don't insert Distinct for a
        # logical project. This is BROKEN.
        # distinct = algebra.Distinct(colSelect)
        # return distinct
        return colSelect


def is_column_equality_comparison(cond):
    """Return a tuple of column indexes if the condition is an equality test.
    """

    if (isinstance(cond, expression.EQ) and
            isinstance(cond.left, UnnamedAttributeRef) and
            isinstance(cond.right, UnnamedAttributeRef)):
        return cond.left.position, cond.right.position
    else:
        return None


class PushApply(rules.Rule):
    """Many Applies in MyriaL are added to select fewer columns from the
    input. In some  of these cases, we can do less work in the children by
    preventing them from producing columns we will then immediately drop.

    Currently, this rule:
      - merges consecutive Apply operations into one Apply, possibly dropping
        some of the produced columns along the way.
      - makes ProjectingJoin only produce columns that are later read.
        TODO: drop the Apply if the column-selection pushed into the
        ProjectingJoin is everything the Apply was doing. See note below.
    """

    def fire(self, op):
        if not isinstance(op, algebra.Apply):
            return op

        child = op.input

        if isinstance(child, algebra.Apply):
            in_scheme = child.scheme()
            child_in_scheme = child.input.scheme()
            names, emits = zip(*op.emitters)
            emits = [to_unnamed_recursive(e, in_scheme)
                     for e in emits]
            child_emits = [to_unnamed_recursive(e[1], child_in_scheme)
                           for e in child.emitters]

            def convert(n):
                if isinstance(n, expression.UnnamedAttributeRef):
                    n = child_emits[n.position]
                else:
                    n.apply(convert)
                return n

            emits = [convert(copy.deepcopy(e)) for e in emits]

            new_apply = algebra.Apply(emitters=zip(names, emits),
                                      input=child.input)
            return self.fire(new_apply)

        elif isinstance(child, algebra.ProjectingJoin):
            in_scheme = child.scheme()
            names, emits = zip(*op.emitters)
            emits = [to_unnamed_recursive(e, in_scheme)
                     for e in emits]
            accessed = sorted(set(itertools.chain(*(accessed_columns(e)
                                                    for e in emits))))
            index_map = {a: i for (i, a) in enumerate(accessed)}
            child.output_columns = [child.output_columns[i] for i in accessed]
            for e in emits:
                expression.reindex_expr(e, index_map)
            # TODO(dhalperi) we may not need the Apply if all it did was rename
            # and/or select certain columns. Figure out these cases and omit
            # the Apply
            return algebra.Apply(emitters=zip(names, emits),
                                 input=child)

        return op

    def __str__(self):
        return 'Push Apply into Apply, ProjectingJoin'


class RemoveUnusedColumns(rules.Rule):
    """For operators that construct new tuples (e.g., GroupBy or Join), we are
    guaranteed that any columns from an input tuple that are ignored (neither
    used internally nor to produce the output columns) cannot be used higher
    in the query tree. For these cases, this rule will prepend an Apply that
    keeps only the referenced columns. The goal is that after this rule,
    a subsequent invocation of PushApply will be able to push that
    column-selection operation further down the tree."""

    def fire(self, op):
        if isinstance(op, algebra.GroupBy):
            child = op.input
            child_scheme = child.scheme()
            grp_list = [to_unnamed_recursive(g, child_scheme)
                        for g in op.grouping_list]
            agg_list = [to_unnamed_recursive(a, child_scheme)
                        for a in op.aggregate_list]
            agg = [accessed_columns(a) for a in agg_list]
            pos = [g.position for g in grp_list]
            accessed = sorted(set(itertools.chain(*(agg + [pos]))))
            if not accessed:
                # Bug #207: COUNTALL() does not access any columns. So if the
                # query is just a COUNT(*), we would generate an empty Apply.
                # If this happens, just keep the first column of the input.
                accessed = [0]
            if len(accessed) != len(child_scheme):
                emitters = [(None, UnnamedAttributeRef(i)) for i in accessed]
                new_apply = algebra.Apply(emitters, child)
                index_map = {a: i for (i, a) in enumerate(accessed)}
                for agg_expr in itertools.chain(grp_list, agg_list):
                    expression.reindex_expr(agg_expr, index_map)
                op.grouping_list = grp_list
                op.aggregate_list = agg_list
                op.input = new_apply
                return op
        elif isinstance(op, algebra.ProjectingJoin):
            l_scheme = op.left.scheme()
            r_scheme = op.right.scheme()
            in_scheme = l_scheme + r_scheme
            condition = to_unnamed_recursive(op.condition, in_scheme)
            column_list = [to_unnamed_recursive(c, in_scheme)
                           for c in op.output_columns]

            accessed = (accessed_columns(condition)
                        | set(c.position for c in op.output_columns))
            if len(accessed) == len(in_scheme):
                return op

            accessed = sorted(accessed)
            left = [a for a in accessed if a < len(l_scheme)]
            if len(left) < len(l_scheme):
                emits = [(None, UnnamedAttributeRef(a)) for a in left]
                apply = algebra.Apply(emits, op.left)
                op.left = apply
            right = [a - len(l_scheme) for a in accessed
                     if a >= len(l_scheme)]
            if len(right) < len(r_scheme):
                emits = [(None, UnnamedAttributeRef(a)) for a in right]
                apply = algebra.Apply(emits, op.right)
                op.right = apply
            index_map = {a: i for (i, a) in enumerate(accessed)}
            expression.reindex_expr(condition, index_map)
            [expression.reindex_expr(c, index_map) for c in column_list]
            op.condition = condition
            op.output_columns = column_list
            return op

        return op

    def __str__(self):
        return 'Remove unused columns'


class PushSelects(rules.Rule):
    """Push selections."""

    @staticmethod
    def descend_tree(op, cond):
        """Recursively push a selection condition down a tree of operators.

        :param op: The root of an operator tree
        :type op: raco.algebra.Operator
        :type cond: The selection condition
        :type cond: raco.expression.expression

        :return: A (possibly modified) operator.
        """

        if isinstance(op, algebra.Select):
            # Keep pushing; selects are commutative
            op.input = PushSelects.descend_tree(op.input, cond)
            return op
        elif isinstance(op, algebra.CompositeBinaryOperator):
            # Joins and cross-products; consider conversion to an equijoin
            left_len = len(op.left.scheme())
            accessed = accessed_columns(cond)
            in_left = [col < left_len for col in accessed]
            if all(in_left):
                # Push the select into the left sub-tree.
                op.left = PushSelects.descend_tree(op.left, cond)
                return op
            elif not any(in_left):
                # Push into right subtree; rebase column indexes
                expression.rebase_expr(cond, left_len)
                op.right = PushSelects.descend_tree(op.right, cond)
                return op
            else:
                # Selection includes both children; attempt to create an
                # equijoin condition
                cols = is_column_equality_comparison(cond)
                if cols:
                    return op.add_equijoin_condition(cols[0], cols[1])
        elif isinstance(op, algebra.Apply):
            # Convert accessed to a list from a set to ensure consistent order
            accessed = list(accessed_columns(cond))
            accessed_emits = [op.emitters[i][1] for i in accessed]
            if all(isinstance(e, expression.AttributeRef)
                   for e in accessed_emits):
                unnamed_emits = [expression.toUnnamed(e, op.input.scheme())
                                 for e in accessed_emits]
                # This condition only touches columns that are copied verbatim
                # from the child, so we can push it.
                index_map = {a: e.position
                             for (a, e) in zip(accessed, unnamed_emits)}
                expression.reindex_expr(cond, index_map)
                op.input = PushSelects.descend_tree(op.input, cond)
                return op
        elif isinstance(op, algebra.GroupBy):
            # Convert accessed to a list from a set to ensure consistent order
            accessed = list(accessed_columns(cond))
            if all((a < len(op.grouping_list)) for a in accessed):
                accessed_grps = [op.grouping_list[a] for a in accessed]
                # This condition only touches columns that are copied verbatim
                # from the child (grouping keys), so we can push it.
                assert all(isinstance(e, expression.AttributeRef)
                           for e in op.grouping_list)
                unnamed_grps = [expression.toUnnamed(e, op.input.scheme())
                                for e in accessed_grps]
                index_map = {a: e.position
                             for (a, e) in zip(accessed, unnamed_grps)}
                expression.reindex_expr(cond, index_map)
                op.input = PushSelects.descend_tree(op.input, cond)
                return op

        # Can't push any more: instantiate the selection
        new_op = algebra.Select(cond, op)
        new_op.has_been_pushed = True
        return new_op

    def fire(self, op):
        if not isinstance(op, algebra.Select):
            return op
        if op.has_been_pushed:
            return op

        new_op = PushSelects.descend_tree(op.input, op.condition)

        # The new root may also be a select, so fire the rule recursively
        return self.fire(new_op)

    def __str__(self):
        return ("Select, Cross/Join => Join;"
                + " Select, Apply => Apply, Select;"
                + " Select, GroupBy => GroupBy, Select")


class RemoveTrivialSequences(rules.Rule):
    def fire(self, expr):
        if not isinstance(expr, algebra.Sequence):
            return expr

        if len(expr.args) == 1:
            return expr.args[0]
        else:
            return expr


class MergeToNaryJoin(rules.Rule):
    """Merge consecutive binary join into a single multiway join
       Note: this code assume that the binary joins form a left deep tree
       before the merge
    """
    @staticmethod
    def mergable(op):
        """ Check if an operator is mergable to Nary join.
            An operator will be merged to Nary Join if its subtree contains
            only join
        """
        allowed_itermediate_types = (algebra.ProjectingJoin, algebra.Select)
        if issubclass(type(op), algebra.ZeroaryOperator):
            return True
        if not isinstance(op, allowed_itermediate_types):
            return False
        elif issubclass(type(op), algebra.UnaryOperator):
            return MergeToNaryJoin.mergable(op.input)
        elif issubclass(type(op), algebra.BinaryOperator):
            return (MergeToNaryJoin.mergable(op.left) and
                    MergeToNaryJoin.mergable(op.right))

    @staticmethod
    def collect_join_groups(op, conditions, children):
        assert isinstance(op, algebra.ProjectingJoin)
        assert (isinstance(op.right, algebra.Select)
                or issubclass(type(op.right), algebra.ZeroaryOperator))
        children.append(op.right)
        conjuncs = expression.extract_conjuncs(op.condition)
        for cond in conjuncs:
            conditions.get_or_insert(cond.left)
            conditions.get_or_insert(cond.right)
            conditions.union(cond.left, cond.right)
        scan_then_select = (isinstance(op.left, algebra.Select) and
                            isinstance(op.left.input, algebra.ZeroaryOperator))
        if (scan_then_select or
                issubclass(type(op.left), algebra.ZeroaryOperator)):
            children.append(op.left)
        else:
            assert isinstance(op.left, algebra.ProjectingJoin)
            MergeToNaryJoin.collect_join_groups(op.left, conditions, children)

    @staticmethod
    def merge_to_multiway_join(op):
        # if it is only binary join, return
        if not isinstance(op.left, algebra.ProjectingJoin):
            return op
        # if it is not mergable, e.g. aggregation along the path, return
        if not MergeToNaryJoin.mergable(op):
            return op
        # do the actual merge
        # 1. collect join groups
        join_groups = UnionFind()
        children = []
        MergeToNaryJoin.collect_join_groups(
            op, join_groups, children)
        # 2. extract join groups from the union find datastructure
        join_conds = defaultdict(list)
        for field, key in join_groups.parents.items():
            join_conds[key].append(field)
        conditions = [v for (k, v) in join_conds.items()]
        # Note: a cost based join order optimization need to be implemented.
        ordered_conds = sorted(conditions, key=lambda cond: min(cond))
        # 3. reverse the children due to top-down tree traversal
        return algebra.NaryJoin(
            list(reversed(children)), ordered_conds, op.output_columns)

    def fire(self, op):
        if not isinstance(op, algebra.ProjectingJoin):
            return op
        else:
            return MergeToNaryJoin.merge_to_multiway_join(op)


class GetCardinalities(rules.Rule):
    """ get cardinalities information of Zeroary operators.
    """
    def __init__(self, catalog):
        assert isinstance(catalog, MyriaCatalog)
        self.catalog = catalog

    def fire(self, expr):
        # if not Zeroary operator, who cares?
        if not issubclass(type(expr), algebra.ZeroaryOperator):
            return expr

        if issubclass(type(expr), algebra.Scan):
            rel = expr.relation_key
            expr._cardinality = self.catalog.num_tuples(rel)
            return expr
        expr._cardinality = 10  # this is a magic number
        return expr

# logical groups of catalog transparent rules
# 1. this must be applied first
remove_trivial_sequences = [RemoveTrivialSequences()]

# 2. simple group by
simple_group_by = [rules.SimpleGroupBy()]

# 3. push down selection
push_select = [
    SplitSelects(),
    PushSelects(),
    MergeSelects()
]

# 4. push projection
push_project = [
    rules.ProjectingJoin(),
    rules.JoinToProjectingJoin()
]

# 5. push apply
push_apply = [
    # These really ought to be run until convergence.
    # For now, run twice and finish with PushApply.
    PushApply(),
    RemoveUnusedColumns(),
    PushApply(),
    RemoveUnusedColumns(),
    PushApply(),
]

# 6. shuffle logics, hyper_cube_shuffle_logic is only used in HCAlgebra
left_deep_tree_shuffle_logic = [
    ShuffleBeforeDistinct(),
    ShuffleBeforeSetop(),
    ShuffleBeforeJoin(),
    BroadcastBeforeCross()
]

# 7. distributed groupby
# this need to be put after shuffle logic
distributed_group_by = [
    # DistributedGroupBy may introduce a complex GroupBy,
    # so we must run SimpleGroupBy after it. TODO no one likes this.
    DistributedGroupBy(), rules.SimpleGroupBy(),
    ProjectToDistinctColumnSelect()
]

# 8. Myriafy logical operators
# replace logical operator with its corresponding Myra operators
myriafy = [
    rules.OneToOne(algebra.CrossProduct, MyriaCrossProduct),
    rules.OneToOne(algebra.Store, MyriaStore),
    rules.OneToOne(algebra.StoreTemp, MyriaStoreTemp),
    rules.OneToOne(algebra.StatefulApply, MyriaStatefulApply),
    rules.OneToOne(algebra.Apply, MyriaApply),
    rules.OneToOne(algebra.Select, MyriaSelect),
    rules.OneToOne(algebra.Distinct, MyriaDupElim),
    rules.OneToOne(algebra.Shuffle, MyriaShuffle),
    rules.OneToOne(algebra.HyperCubeShuffle, MyriaHyperShuffle),
    rules.OneToOne(algebra.Collect, MyriaCollect),
    rules.OneToOne(algebra.ProjectingJoin, MyriaSymmetricHashJoin),
    rules.OneToOne(algebra.NaryJoin, MyriaLeapFrogJoin),
    rules.OneToOne(algebra.Scan, MyriaScan),
    rules.OneToOne(algebra.ScanTemp, MyriaScanTemp),
    rules.OneToOne(algebra.SingletonRelation, MyriaSingleton),
    rules.OneToOne(algebra.EmptyRelation, MyriaEmptyRelation),
    rules.OneToOne(algebra.UnionAll, MyriaUnionAll),
    rules.OneToOne(algebra.Difference, MyriaDifference),
    rules.OneToOne(algebra.OrderBy, MyriaInMemoryOrderBy),
]

# 9. break communication boundary
# get producer/consumer pair
break_communication = [
    BreakHyperCubeShuffle(),
    BreakShuffle(),
    BreakCollect(),
    BreakBroadcast(),
]


class MyriaAlgebra(object):
    """ Myria algebra abstract class
    """
    language = MyriaLanguage

    operators = [
        MyriaSymmetricHashJoin,
        MyriaSelect,
        MyriaScan,
        MyriaStore
    ]

    fragment_leaves = (
        MyriaShuffleConsumer,
        MyriaCollectConsumer,
        MyriaBroadcastConsumer,
        MyriaHyperShuffleConsumer,
        MyriaScan,
        MyriaScanTemp
    )

    @abstractmethod
    def opt_rules(self):
        """ Specific myria algebra must instantiate this method. """


class MyriaLeftDeepTreeAlgebra(MyriaAlgebra):
    """ Myria phyiscal algebra using left deep tree pipeline and 1-D shuffle
    """
    rule_grps_sequence = [
        remove_trivial_sequences,
        simple_group_by,
        push_select,
        push_project,
        push_apply,
        left_deep_tree_shuffle_logic,
        distributed_group_by,
        myriafy,
        break_communication
    ]

    def opt_rules(self):
        return reduce(add, self.rule_grps_sequence, [])


class MyriaHyperCubeAlgebra(MyriaAlgebra):
    """ Myria phyiscal algebra using hyper cube shuffle and LeapFrogJoin
    """
    def opt_rules(self):
        # this rule is hyper cube shuffle specific
        merge_to_nary_join = [
            MergeToNaryJoin()
        ]

        # catalog aware hc shuffle rules, so put them here
        hyper_cube_shuffle_logic = [
            GetCardinalities(self.catalog),
            HCShuffleBeforeNaryJoin(self.catalog),
            OrderByBeforeNaryJoin(),
        ]

        rule_grps_sequence = [
            remove_trivial_sequences,
            simple_group_by,
            push_select,
            push_project,
            merge_to_nary_join,
            push_apply,
            left_deep_tree_shuffle_logic,
            distributed_group_by,
            hyper_cube_shuffle_logic,
            myriafy,
            break_communication
        ]
        return reduce(add, rule_grps_sequence, [])

    def __init__(self, catalog=None):
        self.catalog = catalog


class OpIdFactory(object):
    def __init__(self):
        self.count = 0

    def alloc(self):
        ret = self.count
        self.count += 1
        return ret

    def getter(self):
        return lambda: self.alloc()


def label_op_to_op(label, op):
    """If needed, insert a Store above the op with the relation name label"""
    if isinstance(op, (algebra.Store, algebra.StoreTemp)):
        # Already a store, we're done
        return op

    if not label:
        raise ValueError(
            'label must be a non-empty string, {} {}'.format(label, op))

    return MyriaStoreTemp(input=op, name=label)


def op_list_to_operator(physical_plan):
    """Given a Datalog-style list (label, root_operator) of IDBs,
    add a Store operator to name the output of that operator the
    corresponding label. Gracefully handle the missing label or present Store
    cases."""
    if len(physical_plan) == 1:
        (label, op) = physical_plan[0]
        return label_op_to_op(label, op)

    return algebra.Parallel(label_op_to_op(l, o) for (l, o) in physical_plan)


def compile_fragment(frag_root):
    """Given a root operator, produce a SubQueryEncoding."""

    # A dictionary mapping each object to a unique, object-dependent id.
    # Since we want this to be truly unique for each object instance, even if
    # two objects are equal, we use id(obj) as the key.
    opid_factory = OpIdFactory()
    op_ids = defaultdict(opid_factory.getter())

    def one_fragment(rootOp):
        """Given an operator that is the root of a query fragment/plan, extract
        the operators in the fragment. Assembles a list cur_frag of the
        operators in the current fragment, in preorder from the root.

        This operator also assembles a queue of the discovered roots of later
        fragments, e.g., when there is a ShuffleProducer below. The list of
        operators that should be treated as fragment leaves is given by
        MyriaAlgebra.fragment_leaves. """

        # The current fragment starts with the current root
        cur_frag = [rootOp]
        # Initially, there are no new roots discovered below leaves of this
        # fragment.
        queue = []
        if isinstance(rootOp, MyriaAlgebra.fragment_leaves):
            # The current root operator is a fragment leaf, such as a
            # ShuffleProducer. Append its children to the queue of new roots.
            for child in rootOp.children():
                queue.append(child)
        else:
            # Otherwise, the children belong in this fragment. Recursively go
            # discover their fragments, including the queue of roots below
            # their children.
            for child in rootOp.children():
                (child_frag, child_queue) = one_fragment(child)
                # Add their fragment onto this fragment
                cur_frag += child_frag
                # Add their roots-of-next-fragments into our queue
                queue += child_queue
        return (cur_frag, queue)

    def fragments(rootOp):
        """Given the root of a query plan, recursively determine all the
        fragments in it."""
        # The queue of fragment roots. Initially, just the root of this query
        queue = [rootOp]
        ret = []
        while len(queue) > 0:
            # Get the next fragment root
            rootOp = queue.pop(0)
            # .. recursively learn the entire fragment, and any newly
            # discovered roots.
            (op_frag, op_queue) = one_fragment(rootOp)
            # .. Myria JSON expects the fragment operators in reverse order,
            # i.e., root at the bottom.
            ret.append(reversed(op_frag))
            # .. and collect the newly discovered fragment roots.
            queue.extend(op_queue)
        return ret

    def call_compile_me(op):
        "A shortcut to call the operator's compile_me function."
        op_id = op_ids[id(op)]
        child_op_ids = [op_ids[id(child)] for child in op.children()]
        op_dict = op.compileme(*child_op_ids)
        op_dict['opName'] = op.shortStr()
        assert isinstance(op_id, int), (type(op_id), op_id)
        op_dict['opId'] = op_id
        return op_dict

    # Determine and encode the fragments.
    return [{'operators': [call_compile_me(op) for op in frag]}
            for frag in fragments(frag_root)]


def compile_plan(plan_op):
    subplan_ops = (algebra.Parallel, algebra.Sequence, algebra.DoWhile)
    if not isinstance(plan_op, subplan_ops):
        plan_op = algebra.Parallel([plan_op])

    if isinstance(plan_op, algebra.Parallel):
        frag_list = [compile_fragment(op) for op in plan_op.children()]
        return {"type": "SubQuery",
                "fragments": list(itertools.chain(*frag_list))}

    elif isinstance(plan_op, algebra.Sequence):
        plan_list = [compile_plan(pl_op) for pl_op in plan_op.children()]
        return {"type": "Sequence", "plans": plan_list}

    elif isinstance(plan_op, algebra.DoWhile):
        children = plan_op.children()
        if len(children) < 2:
            raise ValueError('DoWhile must have at >= 2 children: body and condition')  # noqa
        condition = children[-1]
        if isinstance(condition, subplan_ops):
            raise ValueError('DoWhile condition cannot be a subplan op {cls}'.format(cls=condition.__class__))  # noqa
        condition = label_op_to_op('__dowhile_{}_condition'.format(id(
            plan_op)), condition)
        plan_op.args = children[:-1] + [condition]
        body = [compile_plan(pl_op) for pl_op in plan_op.children()]
        condition_lbl = RelationKey.from_string(
            "public:__TEMP__:" + condition.name)
        return {"type": "DoWhile",
                "body": body,
                "condition": relation_key_to_json(condition_lbl)}

    raise NotImplementedError("compiling subplan op {}".format(type(plan_op)))


def compile_to_json(raw_query, logical_plan, physical_plan, catalog=None):
    """This function compiles a physical query plan to the JSON suitable for
    submission to the Myria REST API server. The logical plan is converted to a
    string and passed along unchanged."""

    # raw_query must be a string
    if not isinstance(raw_query, basestring):
        raise ValueError("raw query must be a string")

    # old-style plan with (name, root_op) pair. Turn it into a single operator.
    # If the list has length > 1, it will be a Parallel. Otherwise it will
    # just be the root operator.
    if isinstance(physical_plan, list):
        physical_plan = op_list_to_operator(physical_plan)

    # At this point physical_plan better be a single operator
    if not isinstance(physical_plan, algebra.Operator):
        raise ValueError('Physical plan must be an operator')

    # If the physical_plan is not a SubPlan operator, make it a Parallel
    subplan_ops = (algebra.Parallel, algebra.Sequence, algebra.DoWhile)
    if not isinstance(physical_plan, subplan_ops):
        physical_plan = algebra.Parallel([physical_plan])

    return {"rawDatalog": raw_query,
            "logicalRa": str(logical_plan),
            "plan": compile_plan(physical_plan)}
