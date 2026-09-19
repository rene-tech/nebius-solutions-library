"""Exploratory whole-clip comparison; never certifies physical action validity."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np


def media_identity(path):
    probe = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    video = next(row for row in probe["streams"] if row["codec_type"] == "video")
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "width": video["width"],
        "height": video["height"],
        "frames": int(video["nb_read_frames"]),
        "fps": str(Fraction(video["avg_frame_rate"])),
    }


def decode(path):
    raw = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            "scale=320:240:flags=area",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
    )
    if len(raw) % (320 * 240):
        raise ValueError("Incomplete decoded frame")
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, 240, 320)


def compare_arrays(original, generated):
    if original.shape != generated.shape or original.ndim != 3 or len(original) < 2:
        raise ValueError("Need equal frame geometry/count and at least two frames")
    epes, cosines, source_activity, generated_activity, edge_scores = [], [], [], [], []
    moving_pixels = 0
    for source, output in zip(original, generated, strict=True):
        source_edge = cv2.Canny(source, 70, 140) > 0
        output_edge = cv2.Canny(output, 70, 140) > 0
        kernel = np.ones((5, 5), np.uint8)
        source_dilated = cv2.dilate(source_edge.astype(np.uint8), kernel) > 0
        output_dilated = cv2.dilate(output_edge.astype(np.uint8), kernel) > 0
        precision = float(
            (output_edge & source_dilated).sum() / max(1, output_edge.sum())
        )
        recall = float((source_edge & output_dilated).sum() / max(1, source_edge.sum()))
        edge_scores.append(2 * precision * recall / max(1e-10, precision + recall))
    for index in range(len(original) - 1):
        source_flow = cv2.calcOpticalFlowFarneback(
            original[index], original[index + 1], None, 0.5, 3, 15, 3, 5, 1.2, 0
        )
        output_flow = cv2.calcOpticalFlowFarneback(
            generated[index], generated[index + 1], None, 0.5, 3, 15, 3, 5, 1.2, 0
        )
        source_magnitude, output_magnitude = (
            np.linalg.norm(source_flow, axis=2),
            np.linalg.norm(output_flow, axis=2),
        )
        moving = source_magnitude > 0.75
        moving_pixels += int(moving.sum())
        source_activity.append(float(source_magnitude.mean()))
        generated_activity.append(float(output_magnitude.mean()))
        if moving.any():
            epes.extend(
                np.linalg.norm(source_flow - output_flow, axis=2)[moving].tolist()
            )
            cosine = (source_flow * output_flow).sum(axis=2) / np.maximum(
                source_magnitude * output_magnitude, 1e-6
            )
            cosines.extend(cosine[moving].tolist())
    return {
        "frames": len(original),
        "transitions": len(original) - 1,
        "source_motion_pixels": moving_pixels,
        "flow_endpoint_error_mean_pixels": float(np.mean(epes)) if epes else None,
        "flow_endpoint_error_p90_pixels": float(np.quantile(epes, 0.9))
        if epes
        else None,
        "flow_direction_cosine_mean": float(np.mean(cosines)) if cosines else None,
        "temporal_activity_correlation": float(
            np.corrcoef(source_activity, generated_activity)[0, 1]
        )
        if np.std(source_activity) > 1e-6 and np.std(generated_activity) > 1e-6
        else None,
        "edge_f1_radius2_mean": float(np.mean(edge_scores)),
        "source_activity": source_activity,
        "generated_activity": generated_activity,
    }


def compare(source, output):
    identities = {"source": media_identity(source), "output": media_identity(output)}
    for field in ("width", "height", "frames", "fps"):
        if identities["source"][field] != identities["output"][field]:
            raise ValueError("Source/output media differs before analysis: " + field)
    metrics = compare_arrays(decode(source), decode(output))
    return {
        "schema": "fs2-exploratory-recorded-motion-comparison/v2",
        "identities": identities,
        "method": {
            "opencv_version": cv2.__version__,
            "analysis_dimensions": [320, 240],
            "optical_flow": "Farneback",
            "parameters": [0.5, 3, 15, 3, 5, 1.2, 0],
            "source_motion_threshold_pixels": 0.75,
            "edge_thresholds": [70, 140],
            "edge_match_radius_pixels": 2,
        },
        "metrics": metrics,
        "scientific_scope": {
            "physical_action_alignment_verified": False,
            "visual_contact_appearance_review": "pending separate visual inspection",
            "policy_training_validity_verified": False,
        },
        "limitations": [
            "Exploratory image-space proxy without a calibrated pass threshold.",
            "Lighting and texture changes affect optical-flow estimates.",
            "No calibrated camera, robot masks, contact labels or downstream policy test.",
            "Analysis downsampling does not alter delivered source or output video.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--generated", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.source, args.generated)
    with args.output.open("x") as file:
        json.dump(result, file, indent=2, allow_nan=False)
        file.write("\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "frames": result["metrics"]["frames"],
                "physical_action_alignment_verified": False,
            }
        )
    )


if __name__ == "__main__":
    main()
