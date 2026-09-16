#!/usr/bin/env python3
"""
Base64-encodes each receipt image in benchmarks/images/ into the
benchmarks/images/<name>_b64.txt files that the vision-suite promptfoo
configs (promptfoo-vision-pc.yaml / promptfoo-vision-pi.yaml) reference via
`file://images/<name>_b64.txt`.

Why this is a separate step instead of committing the .txt files directly:
base64 inflates binary data by ~33%, and committing both the image and its
base64 text doubles the repo's storage for no benefit — anyone replicating
the vision suite can regenerate the .txt files locally in under a second.

Usage:
    cd benchmarks
    python ../scripts/encode_images.py

Run it from inside benchmarks/ (or pass --dir to point elsewhere). It reads
every .jpg/.jpeg/.png in images/ and writes a sibling <stem>_b64.txt.
"""
import argparse
import base64
from pathlib import Path

# Maps the image file stem (as it exists in images/) to the exact _b64.txt
# filename the shipped yaml configs expect (a few don't line up 1:1 due to
# filename history/typos in the original benchmark set — kept intentionally
# so the existing yaml `vars:` blocks don't need editing).
FILENAME_MAP = {
    "coffee-receipt": "coffee_receipt_b64.txt",
    "good-guy-restaurant-receipt": "good_guy_restaurant_receipt_b64.txt",
    "Green-grocery-geceipt": "Green_grocery_geceipt_b64.txt",
    "Home-Depot-Receipt": "Home_Depot_Receipt_b64.txt",
    "miami-floridal-receipt": "miami_floridal_receipt_b64.txt",
    "street-food-receipt": "street_food_receipt_b64.txt",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default="images",
        help="Directory containing the source images (default: images, relative to cwd)",
    )
    args = parser.parse_args()

    images_dir = Path(args.dir)
    if not images_dir.is_dir():
        raise SystemExit(
            f"'{images_dir}' is not a directory. Run this from inside benchmarks/, "
            "or pass --dir /path/to/images."
        )

    written = 0
    for image_path in sorted(images_dir.iterdir()):
        if image_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        out_name = FILENAME_MAP.get(image_path.stem)
        if out_name is None:
            print(f"skip (no mapping): {image_path.name}")
            continue
        out_path = images_dir / out_name
        b64 = base64.b64encode(image_path.read_bytes())
        out_path.write_bytes(b64)
        print(f"wrote {out_path} ({len(b64)} bytes)")
        written += 1

    print(f"\nDone — {written} file(s) encoded.")


if __name__ == "__main__":
    main()
