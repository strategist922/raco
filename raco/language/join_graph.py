import networkx as nx
import bisect

from raco.datastructure.ordered_set import OrderedSet


class JoinGraph(object):
    """Represents one or more joins.

    Nodes represent relations; edges represent equijoin conditions.
    """
    def __init__(self, num_nodes):
        """Initialize a join graph."""
        assert num_nodes >= 2
        self.graph = nx.MultiGraph()
        self.graph.add_nodes_from(range(num_nodes))

    def add_edge(self, src_node, dst_node, src_col, dst_col):
        """Add an edge representing an equijoin to the join graph."""
        assert 0 <= src_node < len(self.graph)
        assert 0 <= dst_node < len(self.graph)

        d = {src_node: src_col, dst_node: dst_col}
        _min = min(src_node, dst_node)
        _max = max(src_node, dst_node)
        self.graph.add_edge(_min, _max, cond=(d[_min], d[_max]))

    def choose_left_deep_join_order(self):
        """Chose a left-deep join order.

        Currently, the only goal is to avoid cross-products.
        """

        joined_nodes = OrderedSet()
        graph = self.graph.copy()
        all_nodes = set(self.graph.nodes())

        while len(joined_nodes) < len(graph):
            # Add an arbitrary node to the join set
            for n in all_nodes - set(joined_nodes):
                joined_nodes.add(n)
                break

            # Expand the join set to include all reachable nodes.
            while True:
                new_nodes = set()
                for n1 in joined_nodes:
                    for n2 in self.graph.neighbors_iter(n1):
                        if n2 not in joined_nodes:
                            new_nodes.add(n2)
                joined_nodes |= new_nodes

                if len(new_nodes) == 0:
                    break

        return list(joined_nodes)