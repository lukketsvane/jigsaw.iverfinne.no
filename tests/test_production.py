import json

from shapely.geometry import Polygon, box

from heller_jigsaw.data import corpus
from heller_jigsaw.models.training_state import catalog_digest
from heller_jigsaw.production.audit import lock_check, measure, translation_audit
from heller_jigsaw.production.geometry import tab_curve
from shapely.geometry import LineString


def test_thin_projection_is_detected():
    square = box(0, 0, 40, 40)
    needle = Polygon([(0, 0), (40, 0), (40, 19.5), (60, 19.5),
                      (60, 20.5), (40, 20.5), (40, 40), (0, 40)])
    assert measure(square)["max_tip_loss_mm"] < 1
    assert measure(needle)["max_tip_loss_mm"] > 18


def test_straight_shared_edge_cannot_lock(tmp_path):
    faces = [
        {"exterior": list(box(0, 0, 20, 20).exterior.coords)},
        {"exterior": list(box(20, 0, 40, 20).exterior.coords)},
    ]
    edges = [{"left_face": 0, "right_face": 1, "is_exterior": False,
              "coords": [[20, 0], [20, 20]]}]
    (tmp_path / "partition.json").write_text(json.dumps({"faces": faces, "edges": edges}))
    assert lock_check(edges, 2)["lock_margin_mm"] == 0
    result = translation_audit(tmp_path, ndir=36)
    assert result["unblocked_directions_by_distance"]["3.0"] == 36


def test_undercut_preserves_shared_endpoints():
    points = tab_curve(LineString([(0, 0), (50, 0)]), .5, 1, .65)
    assert points[0] == [0, 0] and points[-1] == [50, 0]
    assert LineString(points).is_simple
    edge = {"left_face": 0, "right_face": 1, "coords": points}
    assert lock_check([edge], 2)["lock_margin_mm"] >= 3


def test_catalog_relocation_keeps_checkpoint_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "__file__", str(tmp_path / "heller_jigsaw/data/corpus.py"))
    path = tmp_path / "runs/catalog.json"
    path.parent.mkdir()
    path.write_bytes(b'[{"asset_id":"sample"}]\n')
    recorded = r"C:\Users\old\Documents\github\jigsaw.iverfinne.no\runs\catalog.json"
    assert corpus.resolve_recorded_path(recorded) == path
    assert corpus.load_corpus_catalog(recorded)[0] == [{"asset_id": "sample"}]
    assert catalog_digest(recorded) == catalog_digest(path)
