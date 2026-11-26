import torch
from shapely.geometry import Polygon, box

from heller_jigsaw.models.shape_prior import AnimalShapeVAE, build_from_checkpoint
from heller_jigsaw.production.fidelity import piece_fidelity
from heller_jigsaw.production.search import CONFIGS, CONFIG_SETS

# A "table" animal: body with two legs and a gap between them.
LEGGED = Polygon([(0, 20), (40, 20), (40, 0), (34, 0), (34, 12), (6, 12), (6, 0), (0, 0)])


def test_identical_piece_scores_perfectly():
    r = piece_fidelity(LEGGED, LEGGED)
    assert r["iou"] > 0.999 and r["boundary_f"] > 0.999
    assert r["bay_fill"] < 0.01 and r["extremity_recall"] > 0.99
    assert r["fidelity"] > 0.99


def test_filled_bay_is_penalised_more_than_area_iou_suggests():
    blob = box(0, 0, 40, 20)
    r = piece_fidelity(blob, LEGGED)
    assert r["bay_fill"] > 0.95
    assert r["excess"] > 0.4


def test_lost_extremity_is_detected():
    needle = Polygon([(0, 0), (30, 0), (30, 14), (48, 14), (48, 16), (30, 16), (30, 30), (0, 30)])
    r = piece_fidelity(box(0, 0, 30, 30), needle)
    # Area IoU barely notices a missing 18 mm spike; the extremity and boundary terms do.
    assert r["iou"] > 0.95
    assert r["extremity_recall"] < 0.25
    assert r["fidelity"] < r["iou"] - 0.15


def test_default_vae_keeps_original_parameter_layout():
    model = AnimalShapeVAE(5, 96, 48)
    names = [k for k in model.state_dict()]
    assert "encoder.2.0.weight" in names and "decoder.6.weight" in names
    assert model.state_dict()["posterior.weight"].shape == (96, 12 * 12 * 64 + 16)
    x = torch.zeros(2, 1, 96, 96)
    y = torch.tensor([0, 1])
    mu, _ = model.encode(x, y)
    assert model.decode(mu, y).shape == x.shape


def test_wider_deeper_vae_round_trips_through_checkpoint():
    model = AnimalShapeVAE(5, 256, 64, width=2, stages=4)
    ckpt = dict(classes=list("abcde"), size=256, latent_dim=64, width=2, stages=4, state_dict=model.state_dict())
    clone = build_from_checkpoint(ckpt)
    clone.load_state_dict(ckpt["state_dict"])
    x = torch.zeros(1, 1, 256, 256)
    y = torch.tensor([3])
    assert clone.decode(clone.encode(x, y)[0], y).shape == x.shape


def test_standard_partition_configs_unchanged():
    assert CONFIG_SETS["standard"] == CONFIGS == [(1, 2, 0), (2, 2, 0), (2, 3, 0), (2, 3, 4), (3, 3, 0), (3, 5, 0)]
    assert CONFIG_SETS["extended"][:6] == CONFIGS


def test_protrusions_find_a_needle_but_not_a_square():
    from heller_jigsaw.production.tips import protrusions

    needle = Polygon([(0, 0), (40, 0), (40, 19.5), (60, 19.5), (60, 20.5), (40, 20.5), (40, 40), (0, 40)])
    assert protrusions(box(0, 0, 40, 40)) == []
    found = protrusions(needle)
    assert found and found[0][0] > 15


def test_interior_spike_is_handed_to_its_neighbour():
    from heller_jigsaw.production.audit import measure
    from heller_jigsaw.production.tips import _interior_proposals

    # Piece 0 sends a 2 mm wide, 14 mm long spike into piece 1.
    a = Polygon([(0, 0), (30, 0), (30, 14), (44, 15), (30, 16), (30, 30), (0, 30)])
    b = box(30, 0, 60, 30).difference(a).buffer(0)
    assert measure(a)["max_tip_loss_mm"] > 6
    props = _interior_proposals(0, [a, b], measure(a)["max_tip_loss_mm"], 6.0)
    assert props
    best = min(props, key=lambda r: r[0])
    qi, qj = best[2], best[3]
    assert measure(qi)["max_tip_loss_mm"] <= 6
    assert abs(qi.area + qj.area - (a.area + b.area)) < 1e-3
