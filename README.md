# Whirlpool Appliances

A custom Home Assistant integration for Whirlpool cloud-connected appliances.

This integration is based on the Whirlpool Android app/API behavior and is intended to support Whirlpool, Maytag, KitchenAid, JennAir, Amana, and related Whirlpool cloud appliances, with extra focus on legacy Whirlpool cooking appliances such as oven/microwave combo units.

> This project is unofficial and is not affiliated with, endorsed by, or supported by Whirlpool Corporation.

## Features

- Home Assistant config flow setup
- Whirlpool account login
- US and EU Whirlpool cloud regions
- Appliance discovery
- Device/entity creation per appliance
- Oven and microwave combo support
- Oven target temperature number entity
- Oven start/modify behavior from the temperature number entity
- Stop oven button
- Stop microwave button
- Cavity light controls
- Microwave light and turntable controls where supported
- Control lock support
- Sabbath mode support where exposed by the appliance
- Raw status diagnostics
- Manual appliance time sync button
- Optional low scan interval support
- SAID shown in device information
- Whirlpool legacy API command services
- Experimental ThingShield/AWS IoT support

## Supported appliance types

This integration currently focuses on Whirlpool cloud appliances discovered from the Whirlpool mobile app API.

Known/targeted categories include:

- Cooking appliances
- Oven/microwave combo units
- Double ovens
- Legacy Minerva cooking models
- Appliances exposing legacy `setAttributes` status/control data

Other appliance categories may partially work if they expose compatible Whirlpool cloud attributes.

## Tested appliance example

Example tested model:

```text
Model: WOC54EC0HS00
Category: Cooking
Data model: DDM_COOKING_MINERVA_COMBO_BIO5_V1
