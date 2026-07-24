# Hardware files

3D-printable parts and CAD sources for the SqueakShot recording rig. For the
full build walkthrough (bill of materials, dimensions, assembly, wiring) see
[../BUILD.md](../BUILD.md).

> **Git LFS:** the `.3mf` and `.f3d` files are tracked with
> [Git LFS](https://git-lfs.com). After cloning, run `git lfs install` once and
> `git lfs pull` to download them.

## Print-ready (`.3mf`)

Print in PLA on the Bambu Lab X1-Carbon (or any FDM printer).

| File | Part |
|------|------|
| `print/recording_housing_parts/LeftSide.3mf` | Housing — left side |
| `print/recording_housing_parts/RightSide.3mf` | Housing — right side |
| `print/recording_housing_parts/LeftStand.3mf` | Housing — left stand |
| `print/recording_housing_parts/RightStand.3mf` | Housing — right stand |
| `print/recording_housing_parts/Lid_Housing.3mf` | Housing — lid |
| `print/raspi_case/Case_Supports.3mf` | Pi case body (with print supports) |
| `print/raspi_case/Lid.3mf` | Pi case lid |
| `print/raspi_case/Buttons.3mf` | Pi case buttons |
| `print/RecordingSetup_3DPrint.3mf` | Combined plate of all parts |

## CAD sources (`.f3d`)

Editable Autodesk Fusion 360 files.

| File | Contents |
|------|----------|
| `cad/FullSetup.f3d` | Full assembly (housing + cage + cameras + Pis) |
| `cad/RaspiCase.f3d` | Raspberry Pi case assembly |
| `cad/LeftSide.f3d`, `cad/RightSide.f3d` | Housing side panels |
| `cad/LeftStand.f3d`, `cad/RightStand.f3d` | Housing stands |
| `cad/Lid_Recording.f3d` | Housing lid |
| `cad/raspi_case/Case_with_Supports.f3d` | Pi case body |
| `cad/raspi_case/Lid.f3d` | Pi case lid |
| `cad/raspi_case/Buttons.f3d` | Pi case buttons |
