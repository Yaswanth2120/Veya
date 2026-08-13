import tempfile
import unittest
from pathlib import Path

from veya.design.state import ArchitectureEdge, ArchitectureNode, ArchitectureState, ArchitectureStore, mermaid
from veya.ipc.errors import ProtocolError


class ArchitectureStateTests(unittest.TestCase):
    def test_persistence_and_derived_mermaid(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ArchitectureStore(Path(temporary))
            saved = store.replace("session-1", ArchitectureState(title="Checkout", nodes=[ArchitectureNode("api", "API"), ArchitectureNode("db", "Database")], edges=[ArchitectureEdge("api", "db", "reads")], decisions=["Use PostgreSQL"]), 1)
            self.assertEqual(saved.version, 2)
            self.assertIn("api -->|reads| db", mermaid(store.get("session-1")))

    def test_edge_without_node_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ProtocolError):
                ArchitectureStore(Path(temporary)).replace("session-1", ArchitectureState(edges=[ArchitectureEdge("api", "db")]), None)
