# DragShift Debian/XFCE end-to-end — 19/19 passed

Xvfb + xfwm4 (compositor off) + Thunar, driving the installed .deb.

| # | Scenario | Result | Detail | Time |
|---|---|---|---|---|
| 1 | JPG → PNG | ✅ | D-Photo.png · 2400×1600 | 9.6s |
| 2 | JPG → WEBP | ✅ | D-Photo.webp · 2400×1600 | 8.9s |
| 3 | JPG → HEIC | ✅ | D-Photo.heic · 2400×1600 | 9.7s |
| 4 | JPG → PDF | ✅ | D-Photo.pdf · 1 page(s) | 8.6s |
| 5 | PNG → JPG | ✅ | D-Graphic.jpg · 800×533 | 8.6s |
| 6 | PDF → JPG (3 pages) | ✅ | 3 × .jpg | 9.7s |
| 7 | PDF → TXT | ✅ | D-Report.txt | 8.6s |
| 8 | DOCX → PDF | ✅ | D-Letter.pdf · 1 page(s) | 8.6s |
| 9 | TXT → PDF | ✅ | D-Notes.pdf · 1 page(s) | 8.6s |
| 10 | PDF Split 1, 2-3 (window) | ✅ | 2 PDFs | 12.4s |
| 11 | PDF Rotate | ✅ | all pages at 90° | 9.4s |
| 12 | PDF Compress | ✅ | 227 KB → 108 KB | 9.7s |
| 13 | Merge 2 PDFs | ✅ | 5 pages | 10.4s |
| 14 | Compress (window, Enter) | ✅ | 354 KB → 112 KB · 69% smaller | 12.3s |
| 15 | Resize 50% (window, Enter) | ✅ | 2400×1600 → 1200×800 | 12.3s |
| 16 | Add BG (window, Enter) | ✅ | 800×533 → 992×725 | 13.3s |
| 17 | Metadata: remove all (Enter) | ✅ | 0 metadata fields left | 12.3s |
| 18 | PDF Protect (password) | ✅ | AES-256, password works | 12.2s |
| 19 | Open With → AVIF (click) | ✅ | D-Photo.avif · 2400×1600 | 8.3s |