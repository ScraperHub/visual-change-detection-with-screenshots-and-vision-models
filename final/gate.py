"""The vision call waits on SSIM or the changed-area ratio."""


def crosses(ssim: float, changed_area: float, ssim_delta: float, area_delta: float) -> bool:
    return (1.0 - ssim) >= ssim_delta or changed_area >= area_delta
