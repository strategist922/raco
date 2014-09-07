
# TODO: To be refactored into parallel shared memory lang,
# where you plugin in the parallel shared memory language specific codegen

from raco import algebra
from raco import expression
from raco.expression import aggregate
from raco.language import Language, Algebra
from raco import rules
from raco.pipelines import Pipelined
from raco.language.clangcommon import StagedTupleRef, ct
from raco.language import clangcommon

from raco.algebra import gensym

import logging
_LOG = logging.getLogger(__name__)

import os.path
import itertools

template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "grappa_templates")


def readtemplate(fname):
    return file(os.path.join(template_path, fname)).read()


base_template = readtemplate("base_query.template")


class GrappaStagedTupleRef(StagedTupleRef):
    def __afterDefinitionCode__(self):
        # Grappa requires structures to be block aligned if they will be
        # iterated over with localizing forall
        return "GRAPPA_BLOCK_ALIGNED"


class GrappaLanguage(Language):
    @classmethod
    def new_relation_assignment(cls, rvar, val):
        return """
    %s
    %s
    """ % (cls.relation_decl(rvar), cls.assignment(rvar, val))

    @classmethod
    def relation_decl(cls, rvar):
        return "GlobalAddress<Tuple> %s;" % rvar

    @classmethod
    def assignment(cls, x, y):
        return "%s = %s;" % (x, y)

    @staticmethod
    def body(compileResult):
        queryexec = compileResult.getExecutionCode()
        initialized = compileResult.getInitCode()
        declarations = compileResult.getDeclCode()
        resultsym = "__result__"
        return base_template % locals()

    @staticmethod
    def log(txt):
        return """LOG(INFO) << "%s";\n""" % txt

    @staticmethod
    def log_unquoted(code, level=0):
        if level == 0:
            log_str = "LOG(INFO)"
        else:
            log_str = "VLOG(%s)" % (level)

        return """%(log_str)s << %(code)s;\n""" % locals()

    @staticmethod
    def group_wrap(ident, grpcode, attrs):
        pipeline_template = ct("""
        Grappa::Metrics::reset();
        auto start_%(ident)s = walltime();
        %(grpcode)s
        auto end_%(ident)s = walltime();
        auto runtime_%(ident)s = end_%(ident)s - start_%(ident)s;
        %(timer_metric)s += runtime_%(ident)s;
        VLOG(1) << "pipeline group %(ident)s: " << runtime_%(ident)s << " s";
        """)

        timer_metric = None
        if attrs['type'] == 'in_memory':
            timer_metric = "in_memory_runtime"
        elif attrs['type'] == 'scan':
            timer_metric = "saved_scan_runtime"

        code = pipeline_template % locals()
        return code

    @staticmethod
    def pipeline_wrap(ident, plcode, attrs):
        code = plcode

        # timing code
        if True:
            inner_code = code
            timing_template = ct("""auto start_%(ident)s = walltime();
            %(inner_code)s
            auto end_%(ident)s = walltime();
            auto runtime_%(ident)s = end_%(ident)s - start_%(ident)s;
            VLOG(1) << "pipeline %(ident)s: " << runtime_%(ident)s << " s";
            VLOG(1) << "timestamp %(ident)s start " << std::setprecision(15)\
             << start_%(ident)s;
            VLOG(1) << "timestamp %(ident)s end " << std::setprecision(15)\
             << end_%(ident)s;
            """)
            code = timing_template % locals()

        syncname = attrs.get('sync')
        if syncname:
            inner_code = code
            sync_template = ct("""spawn(&%(syncname)s, [=] {
                    %(inner_code)s
                    });
                    """)
            code = sync_template % locals()

        syncname = attrs.get('syncdef')
        if syncname:
            inner_code = code
            sync_def_template = ct("""CompletionEvent %(syncname)s;
            %(inner_code)s
            """)
            code = sync_def_template % locals()

        return code

    @staticmethod
    def comment(txt):
        return "// %s\n" % txt

    nextstrid = 0

    @classmethod
    def newstringident(cls):
        r = """str_%s""" % (cls.nextstrid)
        cls.nextstrid += 1
        return r

    @classmethod
    def compile_numericliteral(cls, value):
        return '%s' % (value), [], []

    @classmethod
    def compile_stringliteral(cls, st):
        sid = cls.newstringident()
        decl = """int64_t %s;""" % (sid)
        lookup_init = """auto l_%(sid)s = string_index.string_lookup(%(st)s);
                   on_all_cores([=] { %(sid)s = l_%(sid)s; });""" % locals()
        build_init = """
        string_index = build_string_index("sp2bench_1m.index.medium");
        """

        return """(%s)""" % sid, [decl], [build_init, lookup_init]
        # raise ValueError("String Literals not supported in
        # C language: %s" % s)

    @classmethod
    def negation(cls, input):
        innerexpr, decls, inits = input
        return "(!%s)" % (innerexpr,), decls, inits

    @classmethod
    def negative(cls, input):
        innerexpr, decls, inits = input
        return "(-%s)" % (innerexpr,), decls, inits

    @classmethod
    def expression_combine(cls, args, operator="&&"):
        opstr = " %s " % operator
        conjunc = opstr.join(["(%s)" % arg for arg, _, _ in args])
        decls = reduce(lambda sofar, x: sofar + x, [d for _, d, _ in args])
        inits = reduce(lambda sofar, x: sofar + x, [d for _, _, d in args])
        _LOG.debug("conjunc: %s", conjunc)
        return "( %s )" % conjunc, decls, inits

    @classmethod
    def compile_attribute(cls, expr):
        if isinstance(expr, expression.NamedAttributeRef):
            raise TypeError("""Error compiling attribute reference %s. \
            C compiler only support unnamed perspective. \
            Use helper function unnamed.""" % expr)
        if isinstance(expr, expression.UnnamedAttributeRef):
            symbol = expr.tupleref.name
            # NOTE: this will only work in Selects right now
            position = expr.position
            return '%s.get(%s)' % (symbol, position), [], []


class GrappaOperator (Pipelined):
    language = GrappaLanguage

    def new_tuple_ref(self, sym, scheme):
        return GrappaStagedTupleRef(sym, scheme)


from raco.algebra import UnaryOperator


# TODO: replace with ScanTemp functionality?
class GrappaMemoryScan(algebra.UnaryOperator, GrappaOperator):
    def num_tuples(self):
        return 10000  # placeholder

    def produce(self, state):
        self.input.produce(state)

    # TODO: when have pipeline tree representation,
    # will have a consumeMaterialized() method instead;
    # for now we reuse the tuple-based consume
    def consume(self, inputsym, src, state):
        # generate the materialization from file into memory

        # scan from index
        # memory_scan_template = """forall_localized( %(inputsym)s_index->vs, \
        # %(inputsym)s_index->nv, [](int64_t ai, Vertex& a) {
        #      forall_here_async<&impl::local_gce>( 0, a.nadj, \
        # [=](int64_t start, int64_t iters) {
        #      for (int64_t i=start; i<start+iters; i++) {
        #        auto %(tuple_name)s = a.local_adj[i];
        #
        #          %(inner_plan_compiled)s
        #       } // end scan over %(inputsym)s (for)
        #       }); // end scan over %(inputsym)s (forall_here_async)
        #       }); // end scan over %(inputsym)s (forall_localized)
        #       """

        global_sync_decl_template = ct("""
        GlobalCompletionEvent %(global_syncname)s;
        """)

        global_syncname = gensym()
        state.addDeclarations([global_sync_decl_template % locals()])
        state.setPipelineProperty('global_syncname', global_syncname)

        memory_scan_template = ct("""
    forall<&%(global_syncname)s>( %(inputsym)s.data, %(inputsym)s.numtuples, \
    [=](int64_t i, %(tuple_type)s& %(tuple_name)s) {
    %(inner_plan_compiled)s
    }); // end  scan over %(inputsym)s
    """)

        stagedTuple = state.lookupTupleDef(inputsym)
        tuple_type = stagedTuple.getTupleTypename()
        tuple_name = stagedTuple.name

        inner_plan_compiled = self.parent.consume(stagedTuple, self, state)

        code = memory_scan_template % locals()
        state.setPipelineProperty('type', 'in_memory')
        state.setPipelineProperty('source', self.__class__)
        state.addPipeline(code)
        return None

    def shortStr(self):
        return "%s" % (self.opname())

    def __eq__(self, other):
        """
        For what we are using MemoryScan for, the only use
        of __eq__ is in hashtable lookups for CSE optimization.
        We omit self.schema because the relation_key determines
        the level of equality needed.

        This could break other things, so better may be to
        make a normalized copy of an expression. This could
        include simplification but in the case of Scans make
        the scheme more generic.

        @see FileScan.__eq__
        """
        return UnaryOperator.__eq__(self, other)


class GrappaSymmetricHashJoin(algebra.Join, GrappaOperator):
    _i = 0
    wait_template = ct("""%(syncname)s.wait();
        """)

    @classmethod
    def __genBaseName__(cls):
        name = "%03d" % cls._i
        cls._i += 1
        return name

    def __getHashName__(self):
        name = "dhash_%s" % self.symBase
        return name

    def __getSyncName__(self, side):
        base = "dh_sync_%s" % self.symBase
        if side == "left":
            return base + "_L"
        if side == "right":
            return base + "_R"
        assert False, "type error {left,right}"

    def produce(self, state):
        self.syncnames = []
        self.symBase = self.__genBaseName__()

        if not isinstance(self.condition, expression.EQ):
            msg = "The C compiler can only handle equi-join conditions\
             of a single attribute: %s" % self.condition
            raise ValueError(msg)

        init_template = ct("""%(hashname)s.init_global_DHT( &%(hashname)s, \
        cores()*16*1024 );
                        """)
        declr_template = ct("""typedef DoubleDHT<int64_t, \
                                                   %(left_in_tuple_type)s, \
                                                   %(right_in_tuple_type)s,
                                                std_hash> \
                    DHT_%(left_in_tuple_type)s_%(right_in_tuple_type)s;
      DHT_%(left_in_tuple_type)s_%(right_in_tuple_type)s %(hashname)s;
      """)
        # declaration of hash map
        self._hashname = self.__getHashName__()
        hashname = self._hashname
        self.leftTypeRef = state.createUnresolvedSymbol()
        left_in_tuple_type = self.leftTypeRef.getPlaceholder()
        self.rightTypeRef = state.createUnresolvedSymbol()
        right_in_tuple_type = self.rightTypeRef.getPlaceholder()
        hashdeclr = declr_template % locals()

        state.addDeclarationsUnresolved([hashdeclr])

        self.outTuple = GrappaStagedTupleRef(gensym(), self.scheme())
        out_tuple_type_def = self.outTuple.generateDefinition()
        state.addDeclarations([out_tuple_type_def])

        # find the attribute that corresponds to the right child
        self.rightCondIsRightAttr = \
            self.condition.right.position >= len(self.left.scheme())
        self.leftCondIsRightAttr = \
            self.condition.left.position >= len(self.left.scheme())
        assert self.rightCondIsRightAttr ^ self.leftCondIsRightAttr

        self.right.childtag = "right"
        state.addInitializers([init_template % locals()])
        self.right.produce(state)

        self.left.childtag = "left"
        self.left.produce(state)

        for sn in self.syncnames:
            syncname = sn
            state.addCode(self.wait_template % locals())

    def consume(self, t, src, state):
        access_template = ct("""
        %(hashname)s.insert_lookup_iter_%(side)s<&%(global_syncname)s>(\
        %(keyname)s.get(%(keypos)s), %(keyname)s, \
        [=](%(other_tuple_type)s %(valname)s) {
            join_coarse_result_count++;
            %(out_tuple_type)s %(out_tuple_name)s = \
                             combine<%(out_tuple_type)s, \
                                      %(left_type)s, \
                                      %(right_type)s> (%(left_name)s, \
                            %(right_name)s);
                                %(inner_plan_compiled)s
                                });
                                """)

        hashname = self._hashname
        keyname = t.name
        side = src.childtag

        outTuple = self.outTuple
        out_tuple_type = self.outTuple.getTupleTypename()
        out_tuple_name = self.outTuple.name

        syncname = self.__getSyncName__(src.childtag)
        # only add such a sync if one doesn't exist yet
        if not state.checkPipelineProperty('sync'):
            state.setPipelineProperty('sync', syncname)
            state.setPipelineProperty('syncdef', syncname)
            self.syncnames.append(syncname)

        global_syncname = state.getPipelineProperty('global_syncname')

        if src.childtag == "right":

            # save for later
            self.right_in_tuple_type = t.getTupleTypename()
            state.resolveSymbol(self.rightTypeRef, self.right_in_tuple_type)

            if self.rightCondIsRightAttr:
                keypos = self.condition.right.position \
                    - len(self.left.scheme())
            else:
                keypos = self.condition.left.position \
                    - len(self.left.scheme())

            inner_plan_compiled = self.parent.consume(outTuple, self, state)

            other_tuple_type = self.leftTypeRef.getPlaceholder()
            left_type = other_tuple_type
            right_type = self.right_in_tuple_type
            left_name = gensym()
            right_name = keyname
            self.right_name = right_name
            valname = left_name

            code = access_template % locals()
            return code

        if src.childtag == "left":
            right_in_tuple_type = self.right_in_tuple_type
            left_in_tuple_type = t.getTupleTypename()
            state.resolveSymbol(self.leftTypeRef, left_in_tuple_type)

            if self.rightCondIsRightAttr:
                keypos = self.condition.left.position
            else:
                keypos = self.condition.right.position

            inner_plan_compiled = self.parent.consume(outTuple, self, state)

            left_type = left_in_tuple_type
            right_type = self.right_in_tuple_type
            other_tuple_type = self.right_in_tuple_type
            left_name = keyname
            right_name = gensym()
            valname = right_name

            code = access_template % locals()
            return code

        assert False, "src not equal to left or right"


class GrappaShuffleHashJoin(algebra.Join, GrappaOperator):
    _i = 0
    wait_template = ct("""%(syncname)s.wait();
        """)

    @classmethod
    def __genBaseName__(cls):
        name = "%03d" % cls._i
        cls._i += 1
        return name

    def __getHashName__(self):
        name = "hashjoin_reducer_%s" % self.symBase
        return name

    def __getSyncName__(self, side):
        base = "shj_sync_%s" % self.symBase
        if side == "left":
            return base + "_L"
        if side == "right":
            return base + "_R"
        assert False, "type error {left,right}"

    def produce(self, state):
        self.syncnames = []
        self.symBase = self.__genBaseName__()

        self.right.childtag = "right"
        self.rightTupleTypeRef = None  # may remain None if CSE succeeds
        self.leftTupleTypeRef = None  # may remain None if CSE succeeds

        # find the attribute that corresponds to the right child
        self.rightCondIsRightAttr = \
            self.condition.right.position >= len(self.left.scheme())
        self.leftCondIsRightAttr = \
            self.condition.left.position >= len(self.left.scheme())
        assert self.rightCondIsRightAttr ^ self.leftCondIsRightAttr

        # find right key position
        if self.rightCondIsRightAttr:
            self.right_keypos = self.condition.right.position \
                - len(self.left.scheme())
        else:
            self.right_keypos = self.condition.left.position \
                - len(self.left.scheme())

        # find left key position
        if self.rightCondIsRightAttr:
            self.left_keypos = self.condition.left.position
        else:
            self.left_keypos = self.condition.right.position

        # define output tuple
        outTuple = GrappaStagedTupleRef(gensym(), self.scheme())
        out_tuple_type_def = outTuple.generateDefinition()
        out_tuple_type = outTuple.getTupleTypename()
        out_tuple_name = outTuple.name

        # common index is defined by same right side and same key
        # TODO: probably want also left side
        hashtableInfo = state.lookupExpr((self.right, self.right_keypos))
        if not hashtableInfo:
            # if right child never bound then store hashtable symbol and
            # call right child produce
            self._hashname = self.__getHashName__()
            _LOG.debug("generate hashname %s for %s", self._hashname, self)

            hashname = self._hashname

            # declaration of hash map
            self.rightTupleTypeRef = state.createUnresolvedSymbol()
            self.leftTupleTypeRef = state.createUnresolvedSymbol()
            self.outTupleTypeRef = state.createUnresolvedSymbol()
            right_type = self.rightTupleTypeRef.getPlaceholder()
            left_type = self.leftTupleTypeRef.getPlaceholder()

            # TODO: really want this addInitializers to be addPreCode
            # TODO: *for all pipelines that use this hashname*
            init_template = ct("""
            auto %(hashname)s_num_reducers = cores();
            auto %(hashname)s = allocateJoinReducers\
            <int64_t,%(left_type)s,%(right_type)s,%(out_tuple_type)s>
                (%(hashname)s_num_reducers);
            auto %(hashname)s_ctx = HashJoinContext<int64_t,%(left_type)s,
                %(right_type)s,%(out_tuple_type)s>
                (%(hashname)s, %(hashname)s_num_reducers);""")

            state.addInitializers([init_template % locals()])
            self.right.produce(state)

            self.left.childtag = "left"
            self.left.produce(state)

            state.saveExpr((self.right, self.right_keypos),
                           (self._hashname, right_type, left_type))

            for sn in self.syncnames:
                syncname = sn
                state.addCode(self.wait_template % locals())
        else:
            # if found a common subexpression on right child then
            # use the same hashtable
            self._hashname, right_type, left_type = hashtableInfo
            _LOG.debug("reuse hash %s for %s", self._hashname, self)

        # now that Relation is produced, produce its contents by iterating over
        # the join result
        iterate_template = ct("""MapReduce::forall_symmetric
        <&%(pipeline_sync)s>
        (%(hashname)s, &JoinReducer<int64_t,%(left_type)s,
        %(right_type)s,%(out_tuple_type)s>::resultAccessor,
            [=](%(out_tuple_type)s& %(out_tuple_name)s) {
                 %(inner_code_compiled)s
            });
        """)

        hashname = self._hashname

        state.addDeclarations([out_tuple_type_def])

        global_sync_decl_template = ct("""
        GlobalCompletionEvent %(pipeline_sync)s;
        """)

        pipeline_sync = gensym()

        state.addDeclarations([global_sync_decl_template % locals()])
        state.setPipelineProperty('global_syncname', pipeline_sync)

        # reduce is a single self contained pipeline.
        # future hashjoin implementations may pipeline out of it
        # by passing a continuation to reduceExecute
        reduce_template = ct("""
        %(hashname)s_ctx.reduceExecute();

        """)
        state.addPreCode(reduce_template % locals())

        delete_template = ct("""
            freeJoinReducers(%(hashname)s, %(hashname)s_num_reducers);""")
        state.addPostCode(delete_template % locals())

        inner_code_compiled = self.parent.consume(outTuple, self, state)

        code = iterate_template % locals()
        state.setPipelineProperty('type', 'in_memory')
        state.setPipelineProperty('source', self.__class__)
        state.addPipeline(code)

    def consume(self, inputTuple, fromOp, state):
        if fromOp.childtag == "right":
            side = "Right"

            keypos = self.right_keypos

            self.rightTupleTypename = inputTuple.getTupleTypename()
            if self.rightTupleTypeRef is not None:
                state.resolveSymbol(self.rightTupleTypeRef,
                                    self.rightTupleTypename)
        elif fromOp.childtag == "left":
            side = "Left"

            keypos = self.left_keypos

            self.leftTupleTypename = inputTuple.getTupleTypename()
            if self.leftTupleTypeRef is not None:
                state.resolveSymbol(self.leftTupleTypeRef,
                                    self.leftTupleTypename)
        else:
            assert False, "src not equal to left or right"

        hashname = self._hashname
        keyname = inputTuple.name
        keytype = inputTuple.getTupleTypename()

        # intra-pipeline sync
        global_syncname = state.getPipelineProperty('global_syncname')

        # inter-pipeline sync
        syncname = self.__getSyncName__(fromOp.childtag)
        # only add such a sync if one doesn't exist yet
        if not state.checkPipelineProperty('sync'):
            state.setPipelineProperty('sync', syncname)
            state.setPipelineProperty('syncdef', syncname)
            self.syncnames.append(syncname)

        mat_template = ct("""%(hashname)s_ctx.emitIntermediate%(side)s\
                <&%(global_syncname)s>(\
                %(keyname)s.get(%(keypos)s), %(keyname)s);""")

        # materialization point
        code = mat_template % locals()
        return code


class GrappaGroupBy(algebra.GroupBy, GrappaOperator):
    _i = 0

    @classmethod
    def __genHashName__(cls):
        name = "group_hash_%03d" % cls._i
        cls._i += 1
        return name

    def produce(self, state):
        assert len(self.grouping_list) <= 2, \
            """%s does not currently support \
            "groupings of more than 2 attributes"""\
            % self.__class__.__name__
        assert len(self.aggregate_list) == 1, \
            "%s currently only supports aggregates of 1 attribute"\
            % self.__class__.__name__
        for agg_term in self.aggregate_list:
            assert isinstance(agg_term,
                              expression.BuiltinAggregateExpression), \
                """%s only supports simple aggregate expressions.
                A rule should create Apply[GroupBy]""" \
                % self.__class__.__name__

        self.useKey = len(self.grouping_list) > 0
        _LOG.debug("groupby uses keys? %s" % self.useKey)

        declr_template = None
        if self.useKey:
            if len(self.grouping_list) == 1:
                declr_template = ct("""typedef DHT_symmetric<int64_t, \
                                  int64_t, std_hash> \
                                   DHT_int64;
                """)
            elif len(self.grouping_list) == 2:
                declr_template = ct("""typedef DHT_symmetric<\
                std::pair<int64_t,int64_t>, \
                                  int64_t, pair_hash> \
                                   DHT_pair_int64;
                """)

        self._hashname = self.__genHashName__()
        _LOG.debug("generate hashname %s for %s", self._hashname, self)

        hashname = self._hashname

        if declr_template is not None:
            hashdeclr = declr_template % locals()
            state.addDeclarationsUnresolved([hashdeclr])

        if self.useKey:
            if len(self.grouping_list) == 1:
                init_template = ct("""auto %(hashname)s = \
                DHT_int64::create_DHT_symmetric( );""")
            elif len(self.grouping_list) == 2:
                init_template = ct("""auto %(hashname)s = \
                DHT_pair_int64::create_DHT_symmetric( );""")

        else:
            init_template = ct("""auto %(hashname)s = counter::create();
            """)

        state.addInitializers([init_template % locals()])

        self.input.produce(state)

        # now that everything is aggregated, produce the tuples
        assert len(self.column_list()) == 1 \
            or isinstance(self.column_list()[0],
                          expression.UnnamedAttributeRef), \
            """assumes first column is the key and second is aggregate result
            column_list: %s""" % self.column_list()

        if self.useKey:
            mapping_var_name = gensym()

            if len(self.grouping_list) == 1:
                produce_template = ct("""%(hashname)s->\
                forall_entries<&%(pipeline_sync)s>\
                ([=](std::pair<const int64_t,int64_t>& %(mapping_var_name)s) {
                    %(output_tuple_type)s %(output_tuple_name)s(\
                    {%(mapping_var_name)s.first, %(mapping_var_name)s.second});
                    %(inner_code)s
                    });
                    """)
            elif len(self.grouping_list) == 2:
                produce_template = ct("""%(hashname)s->\
                forall_entries<&%(pipeline_sync)s>\
                ([=](std::pair<const std::pair<int64_t,int64_t>,int64_t>& \
                %(mapping_var_name)s) {
                    %(output_tuple_type)s %(output_tuple_name)s(\
                    {%(mapping_var_name)s.first.first,\
                    %(mapping_var_name)s.first.second,\
                    %(mapping_var_name)s.second});
                    %(inner_code)s
                    });
                    """)
        else:
            op = self.aggregate_list[0].__class__.__name__
            # translations for Grappa::reduce predefined ops
            coll_op = {'COUNT': 'COLL_ADD',
                       'SUM': 'COLL_ADD',
                       'MAX': 'COLL_MAX',
                       'MIN': 'COLL_MIN'}[op]
            produce_template = ct("""auto %(output_tuple_name)s_tmp = \
            reduce<int64_t, \
            counter, \
            %(coll_op)s, \
            &get_count>\
            (%(hashname)s);

            %(output_tuple_type)s %(output_tuple_name)s;
            %(output_tuple_name)s.set(0, %(output_tuple_name)s_tmp);
            %(inner_code)s
            """)

        pipeline_sync_decl_template = ct("""
        GlobalCompletionEvent %(pipeline_sync)s;
        """)

        pipeline_sync = gensym()
        state.setPipelineProperty('global_syncname', pipeline_sync)
        state.addDeclarations([pipeline_sync_decl_template % locals()])

        output_tuple = GrappaStagedTupleRef(gensym(), self.scheme())
        output_tuple_name = output_tuple.name
        output_tuple_type = output_tuple.getTupleTypename()
        state.addDeclarations([output_tuple.generateDefinition()])

        inner_code = self.parent.consume(output_tuple, self, state)
        code = produce_template % locals()
        state.setPipelineProperty("type", "in_memory")
        state.addPipeline(code)

    def consume(self, inputTuple, fromOp, state):
        if self.useKey:
            if len(self.grouping_list) == 1:
                materialize_template = ct("""%(hashname)s->update\
                <&%(pipeline_sync)s, int64_t, \
                &Aggregates::%(op)s<int64_t,int64_t>,0>(\
                %(tuple_name)s.get(%(keypos)s),\
                %(tuple_name)s.get(%(valpos)s));
          """)
                # make key from grouped attributes
                keypos = self.grouping_list[0].get_position(self.scheme())

            elif len(self.grouping_list) == 2:
                materialize_template = ct("""%(hashname)s->update\
                <&%(pipeline_sync)s, int64_t, \
                &Aggregates::%(op)s<int64_t,int64_t>,0>(\
                std::pair<int64_t,int64_t>(\
                %(tuple_name)s.get(%(key1pos)s),\
                %(tuple_name)s.get(%(key2pos)s)),\
                %(tuple_name)s.get(%(valpos)s));
          """)
                # make key from grouped attributes
                key1pos = self.grouping_list[0].get_position(self.scheme())
                key2pos = self.grouping_list[1].get_position(self.scheme())
        else:
            # TODO: use optimization for few keys
            # right now it uses key=0
            materialize_template = ct("""%(hashname)s->count = \
            Aggregates::%(op)s<int64_t, int64_t>(%(hashname)s->count, \
                                      %(tuple_name)s.get(%(valpos)s));
            """)

        hashname = self._hashname
        tuple_name = inputTuple.name
        pipeline_sync = state.getPipelineProperty("global_syncname")

        assert not isinstance(self.aggregate_list[0], aggregate.COUNTALL),\
            """grappalang does not currently support COUNT(*), \
            use COUNT(<attr>)"""

        # get value positions from aggregated attributes
        valpos = self.aggregate_list[0].input.get_position(self.scheme())

        op = self.aggregate_list[0].__class__.__name__

        code = materialize_template % locals()
        return code


class GrappaHashJoin(algebra.Join, GrappaOperator):
    _i = 0

    @classmethod
    def __genHashName__(cls):
        name = "hash_%03d" % cls._i
        cls._i += 1
        return name

    def produce(self, state):
        if not isinstance(self.condition, expression.EQ):
            msg = "The C compiler can only handle equi-join conditions of\
             a single attribute: %s" % self.condition
            raise ValueError(msg)

        declr_template = ct("""typedef MatchesDHT<int64_t, \
                          %(in_tuple_type)s, std_hash> \
                           DHT_%(in_tuple_type)s;
        DHT_%(in_tuple_type)s %(hashname)s;
        """)

        self.right.childtag = "right"
        self.rightTupleTypeRef = None  # may remain None if CSE succeeds

        # find the attribute that corresponds to the right child
        self.rightCondIsRightAttr = \
            self.condition.right.position >= len(self.left.scheme())
        self.leftCondIsRightAttr = \
            self.condition.left.position >= len(self.left.scheme())
        assert self.rightCondIsRightAttr ^ self.leftCondIsRightAttr, \
            "op: %s,\ncondition: %s, left.scheme: %s, right.scheme: %s" \
            % (self, self.condition, self.left.scheme(), self.right.scheme())

        # right key position
        if self.rightCondIsRightAttr:
            self.right_keypos = self.condition.right.position \
                - len(self.left.scheme())
        else:
            self.right_keypos = self.condition.left.position \
                - len(self.left.scheme())

        # left key position
        if self.rightCondIsRightAttr:
            self.left_keypos = self.condition.left.position
        else:
            self.left_keypos = self.condition.right.position

        # common index is defined by same right side and same key
        hashtableInfo = state.lookupExpr((self.right, self.right_keypos))
        if not hashtableInfo:
            # if right child never bound then store hashtable symbol and
            # call right child produce
            self._hashname = self.__genHashName__()
            _LOG.debug("generate hashname %s for %s", self._hashname, self)

            hashname = self._hashname

            # declaration of hash map
            self.rightTupleTypeRef = state.createUnresolvedSymbol()
            in_tuple_type = self.rightTupleTypeRef.getPlaceholder()
            hashdeclr = declr_template % locals()
            state.addDeclarationsUnresolved([hashdeclr])

            init_template = ct("""%(hashname)s.init_global_DHT( &%(hashname)s,
            cores()*16*1024 );""")
            state.addInitializers([init_template % locals()])
            self.right.produce(state)
            state.saveExpr((self.right, self.right_keypos),
                           (self._hashname, self.rightTupleTypename))
            # TODO always safe here? I really want to call
            # TODO saveExpr before self.right.produce(),
            # TODO but I need to get the self.rightTupleTypename cleanly
        else:
            # if found a common subexpression on right child then
            # use the same hashtable
            self._hashname, self.rightTupleTypename = hashtableInfo
            _LOG.debug("reuse hash %s for %s", self._hashname, self)

        self.left.childtag = "left"
        self.left.produce(state)

    def consume(self, t, src, state):
        if src.childtag == "right":

            right_template = ct("""
            %(hashname)s.insert(%(keyname)s.get(%(keypos)s), %(keyname)s);
            """)

            hashname = self._hashname
            keyname = t.name

            keypos = self.right_keypos

            self.rightTupleTypename = t.getTupleTypename()
            if self.rightTupleTypeRef is not None:
                state.resolveSymbol(self.rightTupleTypeRef,
                                    self.rightTupleTypename)

            # materialization point
            code = right_template % locals()

            return code

        if src.childtag == "left":
            left_template = ct("""
            %(hashname)s.lookup_iter<&%(pipeline_sync)s>( \
            %(keyname)s.get(%(keypos)s), \
            [=](%(right_tuple_type)s& %(right_tuple_name)s) {
              join_coarse_result_count++;
              %(out_tuple_type)s %(out_tuple_name)s = \
               combine<%(out_tuple_type)s, \
                       %(keytype)s, \
                       %(right_tuple_type)s> \
                           (%(keyname)s, %(right_tuple_name)s);
              %(inner_plan_compiled)s
            });
     """)

            hashname = self._hashname
            keyname = t.name
            keytype = t.getTupleTypename()

            pipeline_sync = state.getPipelineProperty('global_syncname')

            keypos = self.left_keypos

            right_tuple_name = gensym()
            right_tuple_type = self.rightTupleTypename

            outTuple = GrappaStagedTupleRef(gensym(), self.scheme())
            out_tuple_type_def = outTuple.generateDefinition()
            out_tuple_type = outTuple.getTupleTypename()
            out_tuple_name = outTuple.name

            state.addDeclarations([out_tuple_type_def])

            inner_plan_compiled = self.parent.consume(outTuple, self, state)

            code = left_template % locals()
            return code

        assert False, "src not equal to left or right"


def indentby(code, level):
    indent = " " * ((level + 1) * 6)
    return "\n".join([indent + line for line in code.split("\n")])

#
#
#
# class FreeMemory(GrappaOperator):
#  def fire(self, expr):
#    for ref in noReferences(expr)


# Basic selection like serial C++
class GrappaSelect(clangcommon.CSelect, GrappaOperator):
    pass


# Basic apply like serial C++
class GrappaApply(clangcommon.CApply, GrappaOperator):
    pass


# Basic duplication based bag union like serial C++
class GrappaUnionAll(clangcommon.CUnionAll, GrappaOperator):
    pass


# Basic materialized copy based project like serial C++
class GrappaProject(clangcommon.CProject, GrappaOperator):
    pass


class GrappaFileScan(clangcommon.CFileScan, GrappaOperator):
    ascii_scan_template_GRAPH = """
          {
            tuple_graph tg;
            tg = readTuples( "%(name)s" );

            FullEmpty<GlobalAddress<Graph<Vertex>>> f1;
            privateTask( [&f1,tg] {
              f1.writeXF( Graph<Vertex>::create(tg, /*directed=*/true) );
            });
            auto l_%(resultsym)s_index = f1.readFE();

            on_all_cores([=] {
              %(resultsym)s_index = l_%(resultsym)s_index;
            });
        }
        """

    # C++ type inference cannot infer T in readTuples<T>;
    # we resolve it later, so use %%
    ascii_scan_template = """
    {
    if (FLAGS_bin) {
    %(resultsym)s = readTuplesUnordered<%%(result_type)s>( \
    FLAGS_input_file + ".bin" );
    } else {
    %(resultsym)s.data = readTuples<%%(result_type)s>( \
    FLAGS_input_file, FLAGS_nt);
    %(resultsym)s.numtuples = FLAGS_nt;
    auto l_%(resultsym)s = %(resultsym)s;
    on_all_cores([=]{ %(resultsym)s = l_%(resultsym)s; });
    }
    }
    """

    def __get_ascii_scan_template__(self):
        return self.ascii_scan_template

    def __get_binary_scan_template__(self):
        _LOG.warn("binary not currently supported\
         for GrappaLanguage, emitting ascii")
        return self.ascii_scan_template

    def __get_relation_decl_template__(self, name):
        return """
            DEFINE_string(input_file, "%(name)s", "Input file");
            Relation<%(tuple_type)s> %(resultsym)s;
            """


class GrappaStore(clangcommon.BaseCStore, GrappaOperator):
    def __file_code__(self, t, state):
        filename = (str(self.relation_key).split(":")[2])
        outputnamedecl = """\
        DEFINE_string(output_file, "%s.bin", "Output File");""" % filename
        state.addDeclarations([outputnamedecl])
        names = [x.encode('UTF8') for x in self.scheme().get_names()]
        schemefile = 'writeSchema("%s", "%s", "%s");\n' % \
                     (names, self.scheme().get_types(), filename)
        state.addPreCode(schemefile)
        resultfile = 'writeTuplesUnordered(&result, "%s.bin");' % filename
        state.addPipelineFlushCode(resultfile)

        return ""


class MemoryScanOfFileScan(rules.Rule):
    """A rewrite rule for making a scan into materialization
     in memory then memory scan"""
    def fire(self, expr):
        if isinstance(expr, algebra.Scan) \
                and not isinstance(expr, GrappaFileScan):
            return GrappaMemoryScan(GrappaFileScan(expr.relation_key,
                                                   expr.scheme()))
        return expr

    def __str__(self):
        return "Scan => MemoryScan(FileScan)"


def grappify(join_type, emit_print):
    return [
        rules.ProjectingJoinToProjectOfJoin(),

        rules.OneToOne(algebra.Select, GrappaSelect),
        MemoryScanOfFileScan(),
        rules.OneToOne(algebra.Apply, GrappaApply),
        rules.OneToOne(algebra.Join, join_type),
        rules.OneToOne(algebra.GroupBy, GrappaGroupBy),
        rules.OneToOne(algebra.Project, GrappaProject),
        rules.OneToOne(algebra.UnionAll, GrappaUnionAll),
        # TODO: obviously breaks semantics
        rules.OneToOne(algebra.Union, GrappaUnionAll),
        clangcommon.StoreToBaseCStore(emit_print, GrappaStore),

        clangcommon.BreakHashJoinConjunction(GrappaSelect, join_type)
    ]


class GrappaAlgebra(Algebra):
    language = GrappaLanguage

    def __init__(self, emit_print=clangcommon.EMIT_CONSOLE):
        self.join_type = GrappaHashJoin
        self.emit_print = emit_print

    def opt_rules(self, **kwargs):
        # datalog_rules = [
        #     # rules.removeProject(),
        #     rules.CrossProduct2Join(),
        #     rules.SimpleGroupBy(),
        #     # SwapJoinSides(),
        #     rules.OneToOne(algebra.Select, GrappaSelect),
        #     rules.OneToOne(algebra.Apply, GrappaApply),
        #     # rules.OneToOne(algebra.Scan,MemoryScan),
        #     MemoryScanOfFileScan(),
        #     # rules.OneToOne(algebra.Join, GrappaSymmetricHashJoin),
        #     rules.OneToOne(algebra.Join, self.join_type),
        #     rules.OneToOne(algebra.Project, GrappaProject),
        #     rules.OneToOne(algebra.GroupBy, GrappaGroupBy),
        #     # TODO: this Union obviously breaks semantics
        #     rules.OneToOne(algebra.Union, GrappaUnionAll),
        #     rules.OneToOne(algebra.Store, GrappaStore)
        #     # rules.FreeMemory()
        # ]

        # sequence that works for myrial
        rule_grps_sequence = [
            rules.remove_trivial_sequences,
            rules.simple_group_by,
            clangcommon.clang_push_select,
            rules.push_project,
            rules.push_apply,
            grappify(self.join_type, self.emit_print)
        ]

        if kwargs.get('SwapJoinSides'):
            rule_grps_sequence.insert(0, [rules.SwapJoinSides()])

        return list(itertools.chain(*rule_grps_sequence))

    def set_join_type(self, joinclass):
        self.join_type = joinclass
