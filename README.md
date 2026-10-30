# Tidy Sheets

A small app for vendor inventory spreadsheets. Upload a vendor's file and it:

- **Cleans it up**: finds the header row, removes blank rows, duplicates and totals rows, trims spaces, and reads numbers, prices and dates.
- **Rejects low stock**: products with less than 300 in the quantity column.
- **Remembers each company**: picked from the start of the file name (e.g. `BAZ-…` → The Bazaar), or chosen from the Company menu.
- **Applies that company's bans**: any product whose text contains a banned word or brand.
- **Marks new products**: ones you haven't seen from that company before.
- **Scan on Keepa**: checks each remaining UPC on Amazon and keeps only products that are on Amazon, at least $6.99, at least 40% ROI, and a sales rank of 350,000 or better (products with no sales rank are rejected).
  ROI = (Amazon price − referral fee − FBA fee − Low Sell) ÷ Low Sell, using each product's fees from Keepa (15% + $3.50 when Keepa has none).
  Results never expire, so a product is only scanned once.
- **Reject products by hand**: the Reject button on any product hides it from that company for good, even when the same file comes again. The **You rejected** tab has an Undo button.

Your data (companies, bans, products seen, Keepa key and results) is saved in `~/TidySheetsData/tidy.db` on your computer and is never uploaded to GitHub.

## Install (Mac, once)
Paste this into Terminal and press Enter:

```bash
mkdir -p ~/TidySheets && curl -fsSL https://raw.githubusercontent.com/larrypro1234/tidy-sheets/main/app.py -o ~/TidySheets/app.py && printf '#!/bin/bash\ncd ~/TidySheets && python3 app.py\n' > ~/Desktop/"Tidy Sheets.command" && chmod +x ~/Desktop/"Tidy Sheets.command" && open ~/Desktop/"Tidy Sheets.command"
```

On first run it brings over anything saved in the earlier Tidy Sheets Chrome extension (Keepa key, companies, bans, products seen, Keepa results).

## Running it
Double-click **Tidy Sheets** on the Desktop. The app opens in your browser. Click **Quit app** in the top right to turn it off.

## Updates
The app checks this repository for a newer `VERSION` when it starts (and every 30 minutes while open) and shows an **Update now** bar.
To ship a change: edit `app.py`, increase `VERSION`, and push to `main`.
