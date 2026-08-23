---
name: booking-workflow
description: Safely guide a user through homestay selection, order confirmation, demo payment, and status checks.
---

# Homestay booking workflow

Use this skill when the user wants to book or pay for a homestay.

1. Search with the user's destination, dates, guest count, and preferences.
2. Present the selected homestay, dates, guests, food option, and price.
3. Obtain explicit confirmation before creating the order.
4. Use a fresh idempotency key for the confirmed order request.
5. Explain that demo payment does not contact WeChat or charge money.
6. Obtain separate explicit confirmation before calling demo payment.
7. Query and report the final order status.

Never describe demo payment as a real financial transaction.
