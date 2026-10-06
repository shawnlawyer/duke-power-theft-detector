# Xcel Energy Green Button provider application

Prepared for Epistemic Cognition Inc. on October 6, 2026. This is a working packet, not a submitted application.

## Values ready to transfer to Xcel's form

- Legal company name: Epistemic Cognition Inc.
- Website: https://homeenergywatch.com
- Primary contact: Shawn Lawyer
- Phone: 718-864-2801
- Email: shawn@epistemiccognition.com
- Display name: Home Energy Watch
- Service description (150 characters maximum): Review your Xcel Energy Green Button usage history and keep overlapping interval files in one customer-owned record.
- Service areas: Select only the Xcel service areas that Epistemic Cognition Inc. is prepared to support after confirming coverage with Xcel.

## Values that must be supplied from business records

- Tax Identification Number
- Physical company address
- Mailing address, if different
- State and ZIP for each address
- Primary contact first and last name as used on the application
- Per-service-area contact information, if Xcel requires it for the selected areas

Do not place the tax identifier or private address in this repository. Enter those fields directly into Xcel's current form.

## Current product and data path

Home Energy Watch accepts Green Button ESPI XML through its existing upload flow. It parses `IntervalBlock` and `IntervalReading` values, preserves the source interval duration and units, scopes readings to the selected customer account, and keeps existing readings when a later file overlaps or conflicts. The product currently supports customer download/upload for Xcel; it does not claim a live Xcel Connect My Data connection.

## Submission checklist

1. Confirm the missing legal fields from the company's records.
2. Confirm the service areas and any current Xcel technical or testing requirements.
3. Confirm whether registration, required testing, certification, or ongoing data access has any fee. The reviewed application does not publish a fee schedule.
4. Complete and sign Xcel's current form only after reviewing those answers.
5. Email the completed application to `greenbuttonsupport@xcelenergy.com` only after Shawn authorizes submission.

Source: [Xcel Energy Green Button Program Service Application](https://www.xcelenergy.com/staticfiles/xe-responsive/Partners/Green_Button_Program_Service_Application.pdf). The reviewed form says providers must use Connect My Data and be Green Button compliant, lists Colorado, Michigan, Minnesota, Wisconsin, New Mexico, Texas, North Dakota, and South Dakota, and says processing may take up to ten business days.
