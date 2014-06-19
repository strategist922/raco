import unittest
import json

import raco.fakedb
from raco import RACompiler
from raco.language import MyriaLeftDeepTreeAlgebra, MyriaHyperCubeAlgebra
from raco.myrialang import compile_to_json
from raco.relation_key import RelationKey
from raco.catalog import FakeCatalog


class DatalogTestCase(unittest.TestCase):

    def setUp(self):
        self.db = raco.fakedb.FakeDatabase()

    def execute_query(self, query, myria_algebra):
        '''Run a test query against the fake database'''

        # print query

        dlog = RACompiler()
        dlog.fromDatalog(query)

        assert myria_algebra in [MyriaLeftDeepTreeAlgebra,
                                 MyriaHyperCubeAlgebra]

        if myria_algebra == MyriaLeftDeepTreeAlgebra:
            dlog.optimize(
                target=MyriaLeftDeepTreeAlgebra(),
                eliminate_common_subexpressions=False)
        else:
            dlog.optimize(
                target=MyriaHyperCubeAlgebra(FakeCatalog(64)),
                eliminate_common_subexpressions=False)

        # print dlog.physicalplan

        # test whether we can generate json without errors
        json_string = json.dumps(compile_to_json(
            query, dlog.logicalplan, dlog.physicalplan))
        assert json_string

        op = dlog.physicalplan[0][1]
        output_op = raco.algebra.Store(RelationKey.from_string('__OUTPUT__'),
                                       op)
        self.db.evaluate(output_op)
        return self.db.get_table('__OUTPUT__')

    def check_result(self, query, expected,
                     myria_algebra=MyriaLeftDeepTreeAlgebra):
        '''Execute a test query with an expected output'''
        actual = self.execute_query(query, myria_algebra)
        self.assertEquals(actual, expected)
