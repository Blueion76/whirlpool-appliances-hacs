# Whirlpool appliance support model

This integration is designed to support Whirlpool-family appliances in one integration while staying safe around appliance controls.

## Support levels

### Level 1: Discovered
The appliance appears in Home Assistant and exposes diagnostic metadata.

### Level 2: Read-only
The integration exposes safe read-only status entities such as state, cycle, temperature, door, fault, firmware, and raw status.

### Level 3: Confirmed writable controls
The integration exposes control entities or services only when the command payload is confirmed by at least one of:

- live traffic from the official Whirlpool app
- a DDM capability payload plus a confirmed command shape
- another maintained integration with working code
- a user-provided sanitized command capture

Unknown appliances should not get generic writable controls.

## Adding support for a new appliance

1. Open an Unsupported Whirlpool appliance issue.
2. Attach Home Assistant diagnostics from the Whirlpool Appliances integration.
3. Include model number, appliance type, brand, and region.
4. For writable controls, attach a sanitized command capture from the official app.

## Redaction checklist

Before sharing diagnostics or captures, redact:

- Authorization headers and tokens
- emails/usernames
- account IDs
- SAIDs
- serial numbers
- MAC addresses
- device IDs
- location IDs
