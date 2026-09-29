import unittest
from unittest.mock import patch

from davinci_legacy import fusion_batch


class Port:
    def __init__(self, port_id, direction, connection=None, fail=False):
        self.port_id = port_id
        self.direction = direction
        self.connection = connection
        self.fail = fail

    def GetAttrs(self):
        prefix = "INPS" if self.direction == "input" else "OUTS"
        return {prefix + "_ID": self.port_id, prefix + "_DataType": "Image"}

    def ConnectTo(self, output):
        self.connection = output
        return not self.fail or output is self.original

    def GetConnectedOutput(self):
        return self.connection


class Tool:
    def __init__(self):
        self.input = Port("Input", "input")
        self.output = Port("Output", "output")
        self.deleted = False
        self.data = {}

    def GetInputList(self):
        return {1: self.input}

    def GetOutputList(self):
        return {1: self.output}

    def SetData(self, key, value):
        self.data[key] = value

    def Delete(self):
        self.deleted = True


class FusionBatchTests(unittest.TestCase):
    def test_only_unconnected_image_boundaries_are_exposed(self):
        nodes = {
            "A": {"Inputs": {}},
            "B": {"Inputs": {"Input": {"SourceOp": "A", "Source": "Output"}}},
        }
        self.assertEqual(fusion_batch._references(nodes), [("A", "Output", "B", "Input")])
        self.assertTrue(fusion_batch._reachable(fusion_batch._references(nodes), "A", "B"))
        self.assertFalse(fusion_batch._reachable(fusion_batch._references(nodes), "B", "A"))
        clean = fusion_batch._node_without_connections(nodes["B"])
        self.assertNotIn("SourceOp", clean["Inputs"]["Input"])
        self.assertIn("SourceOp", nodes["B"]["Inputs"]["Input"])

    def test_failed_final_connection_restores_original_chain_and_removes_nodes(self):
        old = Port("Output", "output")
        media_input = Port("Input", "input", old, fail=True)
        media_input.original = old
        node = Tool()
        with patch.object(fusion_batch, "_existing_chain", return_value=(media_input, old)), \
             patch.object(fusion_batch, "_make_tools", return_value={"One": node}), \
             patch.object(fusion_batch, "_connect_internal"):
            with self.assertRaisesRegex(RuntimeError, "接回 MediaOut"):
                fusion_batch._splice(object(), {}, ["One.Input"], "One.Output", "hash")
        self.assertIs(media_input.GetConnectedOutput(), old)
        self.assertTrue(node.deleted)

    def test_success_marks_inserted_graph_for_duplicate_prevention(self):
        old = Port("Output", "output")
        media_input = Port("Input", "input", old)
        node = Tool()
        with patch.object(fusion_batch, "_existing_chain", return_value=(media_input, old)), \
             patch.object(fusion_batch, "_make_tools", return_value={"One": node}), \
             patch.object(fusion_batch, "_connect_internal"):
            fusion_batch._splice(object(), {}, ["One.Input"], "One.Output", "hash")
        self.assertIs(node.input.connection, old)
        self.assertIs(media_input.connection, node.output)
        self.assertEqual(node.data[fusion_batch.MARKER], "hash")


if __name__ == "__main__":
    unittest.main()
