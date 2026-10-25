#!/usr/bin/env python3
"""
fix_image_paths.py
===================
Perbaiki produk yang gambarnya rusak (404) karena diposting lewat versi lama
telegram_admin_bot.py.

Bug lama: bot menyimpan foto ke `static/uploads/<file>` (bukan
`static/uploads/products/<file>`) dan mencatat NAMA DENGAN PREFIX FOLDER
("uploads/<file>") ke kolom image_filename di database — padahal seharusnya
cuma nama file polos, persis seperti yang disimpan form admin biasa.

Skrip ini untuk tiap produk:
  1. Cek 4 kolom gambar (image_filename, image_filename2, image_filename3,
     image_filename4).
  2. Kalau nilainya diawali "uploads/" -> itu record yang rusak akibat bug
     lama. Nama file polosnya diambil dari bagian setelah "uploads/".
  3. Kalau file fisiknya masih ada di lokasi lama (static/uploads/<file>),
     dipindah ke lokasi yang benar (static/uploads/products/<file>).
  4. Kolom di database diperbaiki jadi nama file polos saja.

CARA PAKAI
----------
Jalankan dari folder yang sama dengan app.py (atau sesuaikan import di bawah
kalau app.py ada di lokasi lain).

  # 1) Dry-run dulu (TIDAK mengubah apa-apa, cuma menampilkan apa yang akan
  #    diperbaiki) — SELALU jalankan ini dulu untuk cek:
  python fix_image_paths.py

  # 2) Kalau hasil dry-run sudah sesuai ekspektasi, jalankan sungguhan:
  python fix_image_paths.py --apply
"""

import argparse
import shutil
import sys
from pathlib import Path

IMAGE_FIELDS = ['image_filename', 'image_filename2', 'image_filename3', 'image_filename4']
BROKEN_PREFIX = 'uploads/'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--apply', action='store_true',
        help='Benar-benar jalankan perbaikan (pindah file + update database). '
             'Tanpa flag ini, skrip cuma menampilkan apa yang AKAN diperbaiki (dry-run).'
    )
    args = parser.parse_args()
    dry_run = not args.apply

    try:
        from app import app  # noqa: E402
        from models import db, Product  # noqa: E402
        from app import UPLOAD_BASE, UPLOAD_PRODUCTS  # noqa: E402
    except ImportError as e:
        print(f"❌ Gagal import app.py/models.py: {e}")
        print("   Pastikan skrip ini dijalankan dari folder yang sama dengan app.py.")
        sys.exit(1)

    upload_base = Path(UPLOAD_BASE)
    upload_products = Path(UPLOAD_PRODUCTS)

    mode_label = "DRY-RUN (belum ada perubahan)" if dry_run else "MENERAPKAN PERBAIKAN"
    print(f"=== fix_image_paths.py — {mode_label} ===\n")

    with app.app_context():
        products = Product.query.all()
        total_fields_fixed = 0
        total_files_moved = 0
        total_files_missing = 0
        products_touched = 0

        for product in products:
            product_changed = False

            for field in IMAGE_FIELDS:
                value = getattr(product, field, None)
                if not value or not value.startswith(BROKEN_PREFIX):
                    continue

                bare_filename = value[len(BROKEN_PREFIX):]
                old_path = upload_base / bare_filename          # lokasi lama (bug): static/uploads/<file>
                new_path = upload_products / bare_filename       # lokasi benar: static/uploads/products/<file>

                print(f"Produk #{product.id} [{product.name}] — {field}:")
                print(f"    DB lama : {value!r}")
                print(f"    DB baru : {bare_filename!r}")

                if new_path.exists():
                    print(f"    File sudah ada di lokasi benar: {new_path}")
                elif old_path.exists():
                    print(f"    Pindah file: {old_path}  ->  {new_path}")
                    if not dry_run:
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(old_path), str(new_path))
                    total_files_moved += 1
                else:
                    print(f"    ⚠️  File fisik TIDAK ditemukan di {old_path} maupun {new_path} "
                          f"— hanya kolom database yang diperbaiki, gambar tetap perlu diunggah ulang.")
                    total_files_missing += 1

                if not dry_run:
                    setattr(product, field, bare_filename)

                total_fields_fixed += 1
                product_changed = True
                print()

            if product_changed:
                products_touched += 1

        if not dry_run and total_fields_fixed:
            db.session.commit()

        print("=== Ringkasan ===")
        print(f"Produk terdampak     : {products_touched}")
        print(f"Kolom gambar diperbaiki: {total_fields_fixed}")
        print(f"File dipindahkan     : {total_files_moved}")
        print(f"File fisik hilang    : {total_files_missing}")

        if total_fields_fixed == 0:
            print("\n✅ Tidak ada record dengan format path yang salah. Tidak ada yang perlu diperbaiki.")
        elif dry_run:
            print("\nIni baru simulasi. Jalankan ulang dengan --apply untuk benar-benar memperbaiki.")
        else:
            print("\n✅ Selesai. Database & file sudah diperbaiki.")
            if total_files_missing:
                print(f"⚠️  {total_files_missing} gambar tidak ketemu file fisiknya — perlu diunggah ulang manual lewat admin.")


if __name__ == '__main__':
    main()
