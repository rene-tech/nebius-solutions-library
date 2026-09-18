"""Exercise the real optional dependency used only by VISTA point prompts.

Import-only checks and label-only segmentation miss this postprocessing path.
This CPU build check retains the selected component and removes a disconnected
positive component using MONAI's actual implementation, not a local substitute.
"""
import torch
from monai.transforms.utils import keep_components_with_positive_points


def verify() -> None:
    image = torch.full((1, 1, 8, 8, 8), -1.0)
    image[0, 0, 1:3, 1:3, 1:3] = 1.0
    image[0, 0, 5:7, 5:7, 5:7] = 1.0
    coords = torch.tensor([[[1.0, 1.0, 1.0]]])
    result = keep_components_with_positive_points(image, coords, torch.tensor([[1]]))
    assert torch.all(result[0, 0, 1:3, 1:3, 1:3] > 0)
    assert torch.all(result[0, 0, 5:7, 5:7, 5:7] <= 0)
    assert torch.isfinite(result).all()


if __name__ == "__main__":
    verify()
    print("VISTA positive-point connected-component dependency verified")
