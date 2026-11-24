from types import SimpleNamespace

import pytest

from scripts.convert_image2live2d import preserve_canvas_aspect


@pytest.mark.parametrize(
    ("width", "height", "scale_x", "scale_y"),
    [(1024, 1536, 2 / 3, 1), (1536, 1024, 1, 2 / 3), (1024, 1024, 1, 1)],
)
def test_canvas_aspect_scales_only_model_space_positions(
    width: int, height: int, scale_x: float, scale_y: float
) -> None:
    positions = [(0.75, -0.75), (0.25, 0.5), (0.5, 0.25), (-0.75, 0.75)]
    moc = SimpleNamespace(
        canvas={
            "width": 1000.0,
            "height": 1000.0,
            "originX": 500.0,
            "originY": 500.0,
            "pixelsPerUnit": 500.0,
        },
        sections={
            "keyformPositions": {"xys": positions},
            "artMeshes": {
                "vertexCounts": [1, 1],
                "parentDeformerIndices": [-1, 0],
                "keyformSourcesBeginIndices": [0, 2],
                "keyformSourcesCounts": [2, 1],
            },
            "artMeshKeyforms": {"keyformPositionSourcesBeginIndices": [0, 2, 4]},
            "warpDeformers": {
                "vertexCounts": [1],
                "keyformSourcesBeginIndices": [0],
                "keyformSourcesCounts": [1],
            },
            "warpDeformerKeyforms": {"keyformPositionSourcesBeginIndices": [6]},
        },
    )

    preserve_canvas_aspect(moc, width, height, position_index_unit=2)

    assert moc.canvas["width"] / moc.canvas["height"] == pytest.approx(width / height)
    assert moc.canvas["originX"] == pytest.approx(moc.canvas["width"] / 2)
    assert moc.canvas["originY"] == pytest.approx(moc.canvas["height"] / 2)
    assert moc.canvas["pixelsPerUnit"] == 500.0
    assert positions[0] == pytest.approx((0.75 * scale_x, -0.75 * scale_y))
    assert positions[1] == pytest.approx((0.25 * scale_x, 0.5 * scale_y))
    assert positions[2] == (0.5, 0.25)
    assert positions[3] == pytest.approx((-0.75 * scale_x, 0.75 * scale_y))
