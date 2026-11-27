# Notes for Claude working on Tidy Sheets

## Supplier rules (from the user)
- **Global** (the "global" company in Tidy Sheets): MOQ is **$3,500** by default. When converting or merging Global offer files, fill MOQ with `$3,500` for any item whose sheet gives no MOQ. If the sheet states an MOQ (e.g. "20 cases/SKU & $3,500 Total", "1 Pallet / Location"), keep the sheet's MOQ.
- **Shepher Toys** (shepher.com): our cost is the **Red Dot Special** price, not the Base Price. On shepher.com the Red Dot price is the Shopify `compare_at_price` (the regular `price` is the Base Price).
