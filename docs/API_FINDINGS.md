# Whirlpool Android APK 6.8.3 API findings

Source file inspected: `Whirlpool_6.8.3_apkcombo.com.xapk`. The base APK contained `classes.dex` through `classes8.dex`; the endpoint map below was extracted from DEX string tables and nearby code references.

## Cloud base URLs observed

- `https://api.whrcloud.com`
- `https://api.whrcloud.eu`
- `https://prod-api.whrcloud.eu`
- `https://dev-api.whrcloud.com`
- `https://stage-api.whrcloud.com`
- `https://qa-api.whrcloud.com`
- `https://api-test.whrcloud.com`
- `https://api-test.whrcloud.eu`
- `https://api-stg.whrcloud.eu`
- `https://map.prod.aws.whrcloud.com`
- `https://map.qa.aws.whrcloud.com`
- `https://map.stage.aws.whrcloud.com`

## Local provisioning endpoints observed

- `http://192.168.10.1/connect.cgi`
- `http://192.168.10.1/submit_network`

## Authentication/token strings observed

- `/api/v4/client_auth/accounts`
- `/api/v1/client_auth/accounts`
- `/oauth/custom/authorize`
- `/oauth/token`
- `/api/v1/oauth/revoke-token`
- Token/session storage strings: `KEY_ACCESS_TOKEN`, `KEY_REFRESH_TOKEN`, `KEY_GENERIC_TOKEN`, `KEY_GENERIC_TOKEN_EXPIRATION`, `KEY_MEMBER_TOKEN_EXPIRATION`, `KEY_TOKEN_TYPE_GENERIC`, `KEY_TOKEN_TYPE_MEMBER`.

## Appliance/MQTT code names observed

The APK includes an `IotClient` with methods/strings including `connectToAppliance`, `connectToAppliances`, `publishCommand`, `publishComplementaryCommand`, `publishDishCommand`, `publishGetStatus`, `publishLaundryCommand`, `publishMatterRequest`, `setCavityLight`, `cavityLight`, and `signOut`. The AWS IoT endpoint format string `%s.iot.%s.%s:443` was also present.

## REST paths observed

- `/adrs/api/v2/registrations/accounts/{accountId}`
- `/adrs/api/v2/registrations/replenish/{said}`
- `/adrs/api/v2/registrations/{said}`
- `/api/v1/OptOutStatus`
- `/api/v1/OptOutStatus/{value}`
- `/api/v1/Scan2Cook/favorites/recipe/key/{recipeKey}`
- `/api/v1/ac/getACFilterStatus/{said}`
- `/api/v1/ac/resetACFilter`
- `/api/v1/accessory`
- `/api/v1/accessory/cycle-history`
- `/api/v1/accessory/cycle-history/favorites`
- `/api/v1/accessory/cycle-history/favorites/delete/{favoriteId}`
- `/api/v1/accessory/cycle-history/favorites/notes/{favoriteId}`
- `/api/v1/accessory/cycle-history/favorites/{cycleId}`
- `/api/v1/accessory/cycle-history/lifecycle/`
- `/api/v1/accessory/cycle-history/{cycleId}`
- `/api/v1/accessory/expert/cycle/{cycleId}`
- `/api/v1/accessory/ota`
- `/api/v1/accessory/rangeextender/enable/{cycleId}`
- `/api/v1/accessory/{serialNumber}`
- `/api/v1/account/favorites/entity/cycle`
- `/api/v1/account/favorites/{said}`
- `/api/v1/account/favorites/{said}/entity/cycle/id/{id}`
- `/api/v1/account/privacy`
- `/api/v1/accounts`
- `/api/v1/accounts/update_password`
- `/api/v1/alexaappfallbackurls`
- `/api/v1/appliance/appliances/createReminder`
- `/api/v1/appliance/command`
- `/api/v1/appliance/status/{said}`
- `/api/v1/appliance/{applianceId}`
- `/api/v1/appliance/{said}/sync`
- `/api/v1/appliances/claim/`
- `/api/v1/appliances/unclaim/{said}`
- `/api/v1/appliancesForLoc/{locationId}`
- `/api/v1/channel/{said}`
- `/api/v1/client_auth/PrivacyContent/countryCd/{country}/languageCd/{lang}`
- `/api/v1/client_auth/TncContent/countryCd/{country}/languageCd/{lang}`
- `/api/v1/client_auth/account/validate`
- `/api/v1/client_auth/accounts`
- `/api/v1/client_auth/accounts/forgotPassword/{email}`
- `/api/v1/client_auth/accounts/resend_confirmation/{email}`
- `/api/v1/client_auth/client_version`
- `/api/v1/client_auth/countriesAndStates`
- `/api/v1/client_auth/debug/mobileActivityTraces`
- `/api/v1/client_auth/esp/getpolicydetails`
- `/api/v1/client_auth/getContentFile`
- `/api/v1/client_auth/location`
- `/api/v1/client_auth/privacy_policy`
- `/api/v1/client_auth/sso/login`
- `/api/v1/client_auth/sso/profiles`
- `/api/v1/client_auth/terms_agreement`
- `/api/v1/client_auth/webSocketUrl`
- `/api/v1/cognito/identityid`
- `/api/v1/contents/all/{ddmKey}`
- `/api/v1/dam/assets`
- `/api/v1/dam/product-autocomplete`
- `/api/v1/disconnect/{voiceProvider}/{accountId}`
- `/api/v1/enableAlexaVoice`
- `/api/v1/energy_providers/{zipCode}`
- `/api/v1/esp/{said}`
- `/api/v1/getEnabledFromAlexaStatus/{accountID}`
- `/api/v1/getLocalizedDateAndTimeForSaids`
- `/api/v1/getUserDetails`
- `/api/v1/history/cycle`
- `/api/v1/history/faultCode`
- `/api/v1/live/collect/{said}`
- `/api/v1/localizedDateAndTimeForSaids`
- `/api/v1/location`
- `/api/v1/location/allLocations/account/{accountId}`
- `/api/v1/locations/location/{locationId}`
- `/api/v1/locations/{locationId}/smart_energy`
- `/api/v1/manageRecipe/{said}`
- `/api/v1/messages/{messageId}`
- `/api/v1/oauth/revoke-token`
- `/api/v1/privacy_policy`
- `/api/v1/rms/userdata`
- `/api/v1/rms/userdata/{id}`
- `/api/v1/rms/{app}/drinks`
- `/api/v1/rms/{app}/drinks/{id}`
- `/api/v1/rms/{app}/recipes`
- `/api/v1/rms/{app}/recipes/{id}`
- `/api/v1/rms/{app}/views/{filterName}`
- `/api/v1/send/iocrecipe/{said}`
- `/api/v1/share-accounts`
- `/api/v1/share-accounts/invites`
- `/api/v1/share-accounts/invites/received`
- `/api/v1/share-accounts/invites/received/{shareRequestId}`
- `/api/v1/share-accounts/invites/{id}`
- `/api/v1/sso/profiles`
- `/api/v1/sso/profiles/{ssoAttachProfileId}`
- `/api/v1/ts/ota/descriptor`
- `/api/v1/ts/ota/descriptor/status/{said}`
- `/api/v1/ts/ota/status/{said}`
- `/api/v1/users/{userId}/messages`
- `/api/v1/users/{userId}/messages/{messageId}`
- `/api/v1/voiceEnabledAppliances/{accountID}`
- `/api/v1/wwp/devicelink`
- `/api/v1/wwp/devicelinkstatus`
- `/api/v1/wwp/deviceunlink`
- `/api/v1/wwp/link`
- `/api/v2/DeviceDataModel`
- `/api/v2/Scan2Cook/categories/recipes`
- `/api/v2/Scan2Cook/categories/{category}/recipes`
- `/api/v2/Scan2Cook/recipes/discover/{query}`
- `/api/v2/account/favorites/{said}`
- `/api/v2/checkForFirmwareUpdate/{said}`
- `/api/v2/client_auth/feedback/featureNames`
- `/api/v2/client_auth/whatsNew/{version}`
- `/api/v2/cookRecipe/{said}`
- `/api/v2/feature/cycle/feedback/{said}`
- `/api/v2/feedback/featureAndBug`
- `/api/v2/invitations`
- `/api/v2/invitations/members/{accountId}`
- `/api/v2/invitations/unlink`
- `/api/v2/location/tsApplianceAtLoc`
- `/api/v2/location/{locationId}`
- `/api/v2/marketing_material`
- `/api/v2/notifications/subscriptions/multi`
- `/api/v2/searchRecipe/{upc}`
- `/api/v2/share-accounts`
- `/api/v2/share-accounts/appliances`
- `/api/v2/share-accounts/{accountId}`
- `/api/v2/unRegisterDeviceIdForPush`
- `/api/v2/updateAppliances`
- `/api/v2/user/notification/device/{deviceId}`
- `/api/v3/account/favorites/{said}/{favoriteId}`
- `/api/v3/accounts/{accountId}`
- `/api/v3/appliance/all/account/{accountId}`
- `/api/v3/registerDeviceIDForPush`
- `/api/v4/client_auth/accounts`
- `/oauth/custom/authorize`
- `/oauth/token`
- `/voice/oauth/authorize`

## Integration implementation notes

The generated Home Assistant integration exposes common entities from status data and service wrappers for the major appliance endpoints. Because the APK code is obfuscated and some controls are published over AWS IoT MQTT, the integration also includes `whirlpool_apk.call_api` and `whirlpool_apk.send_appliance_command` so payloads can be adjusted per appliance model without changing Python code.


## ThingShield MQTT details added from bassrock/hass-whirlpool

The `bassrock/hass-whirlpool` project confirms that newer Whirlpool appliances show up as `TS_SAID` in the OAuth response and use AWS IoT MQTT instead of the legacy REST status/command path. The implementation in this package now uses:

- OAuth password or refresh token grant at `/oauth/token` with Android client credentials.
- Cognito identity lookup through `GET /api/v1/cognito/identityid`.
- AWS Cognito `GetCredentialsForIdentity` for temporary AWS credentials.
- AWS IoT websocket/SigV4 MQTT against `wt.applianceconnect.net` for US/NAR and `wt-eu.applianceconnect.net` for EU/EMEA.
- Subscribe topics:
  - `cmd/{model}/{said}/response/{clientId}`
  - `dt/{model}/{said}/state/update`
  - `$aws/events/presence/connected/{said}`
  - `$aws/events/presence/disconnected/{said}`
  - `api/capability/download/{model}/{said}/response`
  - `dt/{model}/{said}/ota/status`
- Publish topic: `cmd/{model}/{said}/request/{clientId}`
- Safe state request payload:

```json
{
  "requestId": "uuid-v4",
  "timestamp": 1773599995000,
  "payload": {
    "addressee": "appliance",
    "command": "getState"
  }
}
```

Known ThingShield washer fields include `washer.applianceState`, `washer.cycleName`, `washer.currentPhase`, `washer.cycleTime.time`, `washer.cycleTime.timeComplete`, `washer.doorStatus`, `washer.doorLockStatus`, `remoteStartEnable`, `hmiControlLockout`, `activeFault`, `faultHistory`, and `systemVersion`.
