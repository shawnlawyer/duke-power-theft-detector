# Xcel Energy Green Button provider registration

Prepared for Epistemic Cognition Inc. on October 6, 2026. Xcel directed us to the current [Green Button vendor form](https://myenergy.xcelenergy.com/greenbutton/green-vendor) and said it is in the process of removing the older PDF application. This packet is not a submitted registration.

## Values ready to transfer to Xcel's current form

- Legal company name: Epistemic Cognition Inc.
- Website: https://homeenergywatch.com
- Primary contact: Shawn Lawyer
- Phone: 718-864-2801
- Email: shawn@epistemiccognition.com
- Display name: Home Energy Watch
- Candidate logo URI (PNG, pending deployment and public HTTP verification): https://app.homeenergywatch.com/static/home-energy-watch-mark.png
- Service description (150 characters maximum): Review your Xcel Energy Green Button usage history and keep overlapping interval files in one customer-owned record.
- Service areas: The current form lists Colorado, Michigan, Minnesota, Wisconsin, New Mexico, Texas, North Dakota, and South Dakota. Select intended coverage directly in Xcel's form.

## Values that must be supplied from business records or the current form

- Tax Identification Number
- Physical company address
- Mailing address, if different
- State and ZIP for each address
- One business contact shown by the current form
- Required logo URI and policy URI
- Required notification URL and redirect URL, only after those endpoints are implemented and verified against the confirmed protocol
- Any new password requested by Xcel, entered directly into Xcel's form and never stored here

Do not place the tax identifier, private address, passwords, tokens, credentials, or secret-bearing URLs in this repository. Public operational URLs may be tracked once their implementation and protocol behavior are verified.

## Current product and data path

Home Energy Watch accepts Green Button ESPI XML through its existing upload flow. It parses `IntervalBlock` and `IntervalReading` values, preserves the source interval duration and units, scopes readings to the selected customer account, and keeps existing readings when a later file overlaps or conflicts. The product currently supports customer download/upload for Xcel; it does not claim a live Xcel Connect My Data connection.

Xcel's October 6, 2026 response confirmed that no Green Button certification is required, Xcel does not charge Green Button Connect fees, and Xcel does not provide a test environment. Those facts do not establish live approval or customer access for Home Energy Watch.

## Submission checklist

1. Complete the current online registration with verified business and contact details.
2. Add the required logo and policy URIs and truthful service-area selections.
3. Implement and verify notification and redirect endpoints against the confirmed protocol before entering their public URLs.
4. Treat the confirmed no-certification/no-fee/no-sandbox facts as onboarding information, not as approval or live access.
5. Submit the current registration only through the authorized owner workflow.

Xcel's current registration form and vendor/service-provider guides are the source for the remaining fields and technical workflow. No callback or notification endpoint is represented as operational in this repository.
