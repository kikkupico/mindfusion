import tempfile
import time
import unittest
from pathlib import Path

from mindfusion.server import DiffusionEngine, StateStore, extract_inline_comments


class TestMindFusionStudio(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp_dir.name) / "state.json"
        self.store = StateStore(self.state_path)
        self.engine = DiffusionEngine(self.store)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_initial_blank_canvas_and_version_tree(self) -> None:
        snap = self.store.get_snapshot()
        self.assertEqual(snap["root_id"], "v0-root")
        self.assertEqual(snap["active_node_id"], "v0-root")
        root = snap["nodes"]["v0-root"]
        self.assertEqual(root["version_num"], 0)
        self.assertEqual(root["content"], "")
        self.assertEqual(root["seed_outline"], "")

    def test_seed_to_polished_generation_and_comment_regeneration_tree(self) -> None:
        # 1. User enters seed outline on v0 and clicks Generate
        job1 = self.store.create_generation_job({
            "parent_id": "v0-root",
            "seed_outline": "# Distributed Cache Engine\n- Low latency edge replication\n- Automatic invalidation policies",
            "content": "",
            "params": {
                "strength": 0.8,
                "guidance_scale": 8.0,
                "steps": 3,
                "style_preset": "Technical Spec & Architecture",
                "negative_prompt": "fluff",
                "provider": "local_synth",
            },
        })
        self.engine._run_job(job1["id"])

        snap1 = self.store.get_snapshot()
        v1_id = snap1["active_node_id"]
        self.assertNotEqual(v1_id, "v0-root")
        v1 = snap1["nodes"][v1_id]
        self.assertEqual(v1["parent_id"], "v0-root")
        self.assertEqual(v1["version_num"], 1)
        self.assertIn("Distributed Cache Engine", v1["content"])
        self.assertGreaterEqual(len(v1["diffusion_steps"]), 2)

        # 2. User adds an inline comment across the generated content and regenerates -> v2
        c1 = self.store.add_comment(v1_id, {
            "quote": "Low latency edge replication",
            "comment": "Expand with sub-5ms p99 SLA metrics",
        })
        self.assertFalse(c1["resolved"])

        job2 = self.store.create_generation_job({
            "parent_id": v1_id,
            "seed_outline": v1["seed_outline"],
            "content": v1["content"],
            "comments": self.store.get_snapshot()["nodes"][v1_id]["comments"],
            "params": {**v1["params"], "provider": "local_synth"},
        })
        self.engine._run_job(job2["id"])

        snap2 = self.store.get_snapshot()
        v2_id = snap2["active_node_id"]
        v2 = snap2["nodes"][v2_id]
        self.assertEqual(v2["parent_id"], v1_id)
        self.assertEqual(v2["version_num"], 2)
        self.assertIn("sub-5ms p99 SLA metrics", v2["content"])
        self.assertTrue(all(c["resolved"] for c in v2["comments"]))

        # 3. User types an inline comment directly in the notepad (e.g. // comment) and taps Diffuse -> v3
        job3 = self.store.create_generation_job({
            "parent_id": v2_id,
            "content": v2["content"] + "\n// Add a short rollout checklist at the end",
            "seed_outline": v2["content"] + "\n// Add a short rollout checklist at the end",
            "params": {**v2["params"], "provider": "local_synth"},
        })
        self.engine._run_job(job3["id"])
        snap3 = self.store.get_snapshot()
        v3_id = snap3["active_node_id"]
        v3 = snap3["nodes"][v3_id]
        self.assertEqual(v3["parent_id"], v2_id)
        self.assertEqual(v3["version_num"], 3)
        self.assertNotIn("// Add a short rollout checklist", v3["content"])

        # 4. User switches back to v1 and forks a branch -> v4 (branching DAG)
        v4 = self.store.create_Manual_Fork(v1_id, {
            "content": v1["content"] + "\n\n## Custom User Section\nHand-edited branch.",
            "diff_summary": "Manual branch from v1",
        })
        self.assertEqual(v4["parent_id"], v1_id)
        self.assertEqual(v4["version_num"], 4)
        # 5. User clicks 'New' -> creates a brand-new diffusion folder without destroying previous work
        first_doc_id = self.store.doc_id
        first_folder = Path(self.store.doc_dir)
        self.assertTrue((first_folder / "tree.json").exists())
        self.assertTrue((first_folder / "v1.html").exists())
        self.assertTrue((first_folder / "latest.html").exists())

        snap_new = self.store.reset_workspace()
        second_doc_id = self.store.doc_id
        self.assertNotEqual(first_doc_id, second_doc_id)
        self.assertEqual(snap_new["active_node_id"], "v0-root")
        self.assertEqual(len(snap_new["nodes"]), 1)
        self.assertEqual(len(snap_new["documents"]), 2)

        # Switching back to the first diffusion folder restores all 5 versions intact
        snap_restored = self.store.load_document(first_doc_id)
        self.assertEqual(snap_restored["doc_id"], first_doc_id)
        self.assertEqual(len(snap_restored["nodes"]), 5)

    def test_html_inline_comments_are_extracted_into_job(self) -> None:
        html = (
            "<h1>Ice House</h1><div>// Enhance with some public domain pics</div>"
            "<p>Built in 1842.<br>&gt;&gt; cite a source<br>Still standing.</p>"
            "<pre><code>// keep this code comment</code></pre>"
        )
        comments, cleaned = extract_inline_comments(html)
        self.assertEqual([c["comment"] for c in comments], ["Enhance with some public domain pics", "cite a source"])
        self.assertEqual(comments[0]["quote"], "Ice House")
        self.assertEqual(comments[1]["quote"], "Built in 1842.")
        self.assertNotIn("Enhance with", cleaned)
        self.assertNotIn("cite a source", cleaned)
        self.assertIn("// keep this code comment", cleaned)
        self.assertIn("Still standing.", cleaned)
        self.assertNotIn("<div></div>", cleaned)

        job = self.store.create_generation_job({"parent_id": "v0-root", "content": html})
        self.assertEqual(len(job["comments"]), 2)
        self.assertEqual(job["working_content"], cleaned)
        # Inline comments live on the job only, never on the parent node's comment list.
        self.assertEqual(self.store.get_snapshot()["nodes"]["v0-root"]["comments"], [])

    def test_edits_to_generated_versions_become_drafts(self) -> None:
        job = self.store.create_generation_job({"parent_id": "v0-root", "content": "seed"})
        v1 = self.store.complete_job(job["id"], "<h1>Original</h1>")
        self.store.update_node(v1["id"], {"content": "<h1>Original</h1><div>// more</div>"})
        node = self.store.get_snapshot()["nodes"][v1["id"]]
        self.assertEqual(node["content"], "<h1>Original</h1>")
        self.assertEqual(node["draft"], "<h1>Original</h1><div>// more</div>")
        # Typing back to the original clears the draft
        self.store.update_node(v1["id"], {"content": "<h1>Original</h1>"})
        self.assertNotIn("draft", self.store.get_snapshot()["nodes"][v1["id"]])
        # Diffusing from a draft consumes it and records it as the child's input
        self.store.update_node(v1["id"], {"content": "<h1>Edited</h1>"})
        job2 = self.store.create_generation_job({"parent_id": v1["id"], "content": "<h1>Edited</h1>"})
        v2 = self.store.complete_job(job2["id"], "<h1>Better</h1>")
        snap = self.store.get_snapshot()
        self.assertNotIn("draft", snap["nodes"][v1["id"]])
        self.assertEqual(v2["input_content"], "<h1>Edited</h1>")
        # Saving a draft as its own version
        self.store.update_node(v2["id"], {"content": "<h1>Hand tuned</h1>"})
        v3 = self.store.create_Manual_Fork(v2["id"], {"content": "<h1>Hand tuned</h1>", "from_draft": True})
        snap = self.store.get_snapshot()
        self.assertNotIn("draft", snap["nodes"][v2["id"]])
        self.assertEqual(snap["nodes"][v3["id"]]["content"], "<h1>Hand tuned</h1>")

    def test_cancel_blocks_late_results_and_allows_new_jobs(self) -> None:
        job = self.store.create_generation_job({"parent_id": "v0-root", "content": "seed"})
        with self.assertRaises(ValueError):
            self.store.create_generation_job({"parent_id": "v0-root", "content": "again"})
        self.assertEqual(self.store.cancel_job(job["id"]), [job["id"]])
        self.store.update_job_step(job["id"], 2, "late step", "x")
        self.assertEqual(self.store.get_job(job["id"])["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.store.complete_job(job["id"], "<p>late</p>")
        self.assertEqual(len(self.store.get_snapshot()["nodes"]), 1)
        self.store.create_generation_job({"parent_id": "v0-root", "content": "again"})

    def test_interrupted_jobs_are_recovered_on_restart(self) -> None:
        job = self.store.create_generation_job({"parent_id": "v0-root", "content": "seed"})
        restarted = StateStore(self.state_path)
        recovered = restarted.get_job(job["id"])
        self.assertEqual(recovered["status"], "error")
        self.assertIn("Interrupted", recovered["error"])

    def test_live_listener_does_not_redeliver_claimed_jobs(self) -> None:
        job = self.store.create_generation_job({"parent_id": "v0-root", "content": "seed"})
        first = self.store.wait_for_pending_job(timeout=0.1)
        self.assertEqual([j["id"] for j in first], [job["id"]])
        self.assertEqual(self.store.wait_for_pending_job(timeout=0.1), [])
        # The engine's "awaiting live session" step must not un-claim it.
        self.store.update_job_step(job["id"], 1, "waiting", "", status="pending", provider_used="awaiting_live_session")
        self.assertEqual(self.store.get_job(job["id"])["provider_used"], "claimed_by_live_session")
        self.assertEqual(self.store.wait_for_pending_job(timeout=0.1), [])
        self.store.register_agent_heartbeat("Claude Code", mode="agent_bridge")
        self.assertTrue(self.store.agent_summary()["live_session"])

    def test_long_poll_and_blank_doc_filtering(self) -> None:
        v = self.store.state_version
        self.assertFalse(self.store.wait_for_change(v, 0.05))
        self.store.update_node("v0-root", {"content": "<p>hello</p>"})
        self.assertTrue(self.store.wait_for_change(v, 0.05))
        self.store.reset_workspace()
        self.store.reset_workspace()  # leaves an untouched blank doc behind
        docs = self.store.get_snapshot()["documents"]
        self.assertEqual(len(docs), 2)  # the "hello" doc + the current blank one
        self.assertTrue(any(d["is_current"] for d in docs))


if __name__ == "__main__":
    unittest.main()
