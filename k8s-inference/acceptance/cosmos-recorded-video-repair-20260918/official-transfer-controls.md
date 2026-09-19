# Official Cosmos transfer controls: public qualification evidence

The three previously unexercised controls—depth, segmentation and whole-scene
motion (WSM)—now return valid full-length videos through the ordinary scientist10
MCP key on release173. This closes a mode-coverage gap; it is not a calibrated
scene reconstruction or robot-policy qualification.

## Immutable public inputs

Inputs and their corresponding prompts come from the official
[NVIDIA Cosmos transfer cookbook](https://github.com/NVIDIA/cosmos/tree/b0e54e88c322695dab188e6ed160c4d6d071c39d/cookbooks/cosmos3/generator/transfer),
revision `b0e54e88c322695dab188e6ed160c4d6d071c39d`, under OpenMDW-1.1.
The license, README, source prompts and original MP4s are retained unchanged.

| Control | Original media | Source MP4 SHA256 |
| --- | --- | --- |
| Depth | 1280×720, 121 frames, 29.97 FPS | `0552c2617a27792cf6416243e8e152839b3686371dc15f4ba3d106317486a87e` |
| Segmentation | 1920×1080, 121 frames, 30 FPS | `cb3faf8eae97efb36586f8bbb6ce54ec90e399f0753b7250f96b11b94a29b64b` |
| WSM | 1280×720, 100 frames, 10 FPS | `614a207c3a29a1e5aac83b7ac82678c04858e2a121bd20b237b5c70f5e60b6ae` |

Each request uses the entire decoded source clip, explicitly requests 512×288
output, and uploads its control reference through the native nested artifact
field `controls[0].reference`. Output rates are 30, 30 and 10 FPS respectively;
the depth example's 29.97→30 conversion is explicit. Aspect ratio is preserved.
The WSM README example requests 101 frames, but the actual pinned file contains
100; the initial 101-frame preparation failed offline and remains retained.
No inference was submitted with that incorrect expectation.

Only the original prompt's `background_setting` and `temporal_caption` fields
are retained to fit unchanged hosted bounds. The long shared negative prompt is
explicitly omitted. All use 35 steps and seed 20260919. Text guidance is 3/3/1 and
control guidance 1.5/2/3 for depth/segmentation/WSM. These are bounded mode tests,
not an exact reproduction of the cookbook's quality settings.

## Public results

| Control | Operation | Output | Output SHA256 |
| --- | --- | --- | --- |
| Depth | `4d9271a9-7c32-4b8d-a55b-5ce6c3cf0ebf` | 512×288, 121 frames, 30 FPS | `8eb85da179c9a379cdda7e1806d5640906e33bedb76578bb672f8adba8bd5bbe` |
| Segmentation | `092aec4c-45ef-4ec4-b0a5-eb6bac062986` | 512×288, 121 frames, 30 FPS | `13babf4da2eb2fb6c52bd3b742ffbf674ac86a71e365c91e336d08dba815dea2` |
| WSM | `4e8279dc-afb6-4c07-9f54-4248bb3ed68c` | 512×288, 100 frames, 10 FPS | `3013d4aa3f32e11794460275e1463c1bf996d0eee5344f8d75566548eca32059` |

Five matching temporal positions were visually inspected for each source/output:

- Depth: the generated street, trees and vehicles follow the source's broad
  layout and forward movement. This does not measure metric depth accuracy.
- Segmentation: the bouldering wall and climber follow the large segmented
  regions and changing climber pose. The wall texture and holds are generated;
  no pixelwise segmentation or exact contact correspondence is claimed.
- WSM: the generated intersection and crossing vehicles follow the rendered
  scene layout and movement. No camera calibration, exact vehicle geometry or
  simulator-state equivalence is established.

The native image remains `sha256:5e2680aa1f8332413638ec1bc962c3796a79a314c1c84f5456d32aa916839e32`;
the strict snapshot owner was not changed for these tests. Per-request actual
runtime attribution and restore witnesses are retained, separately from media
validation. Valid output alone must not be used to invent missing GPU identity.
The depth and segmentation operations have null public Pod/node attribution;
WSM has a joined strict-restore witness. This defect is tracked independently.

## Evidence and limits

Protected campaign directory:
`scientific-qualification-20260918/lerobot-admission-diagnosis/`.

- Frozen fixture manifest: `official-control-matrix-v2/manifest.json`, SHA256
  `6c36d564034fdc6230c9fc4891042260baa97408eea70227714f44f0dad1f22f`.
- Public aggregate: `official-control-public-r1/summary.json`, SHA256
  `2a7b85377102df889032a7b80288699897d249a467cbd0c22af70181f208e4e8`.
- Requests, operation polling, results, decoded-media checks and runtime
  witnesses: `public-mode-official-control-{depth,seg,wsm}-512x288-r1/`.
- Visual grids, each named `conditioning-comparison.png` in those folders:
  depth `7908d79398229f27f76868d2860e89e6ccb046c88c0a70a20fd16fe8d76101a7`,
  segmentation `66ee3c574e77bcb84975141348dc656afc4108e69c59a6cd57de7a70e18c281e`,
  WSM `8de5c9298812e8c85961621e3204b2f10ddf3dd092995a47f2f7a287e709dcac`.

This sequential public MCP test does not qualify burst concurrency, every
possible shape/parameter combination, or the actual LibreChat user path.
It does not establish physical action-label validity for generated robotics
data. Original failures and the separate recorded-ALOHA comparisons remain
part of the release evidence.
