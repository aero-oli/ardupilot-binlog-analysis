# Parameter Metadata Schema

Compact parameter metadata files are JSON documents with this shape:

```json
{
  "metadata_version": "2026-05-curated",
  "source_vehicle": "ArduCopter",
  "source_url": "https://ardupilot.org/copter/docs/parameters.html",
  "caveat": "...",
  "parameters": [
    {
      "name": "WP_YAW_BEHAVIOR",
      "display_name": "Yaw behaviour during missions",
      "description": "Determines how Copter controls yaw during missions and RTL.",
      "units": null,
      "range": [0, 3],
      "values": {"0": "Never change yaw"},
      "bitmask": null,
      "user_level": "Standard",
      "reboot_required": false,
      "source_vehicle": "ArduCopter",
      "metadata_version": "2026-05-curated",
      "source_url": "https://ardupilot.org/copter/docs/parameters.html#wp-yaw-behavior"
    }
  ]
}
```

Only fields useful to log investigation are included: descriptions, units/ranges, enum values, bitmasks, user level, reboot hints, and source/caveat data. Metadata is explanatory context only; it is not firmware-specific proof and must not be used to recommend automatic parameter changes.

`scripts/update_parameter_metadata.py --fetch --vehicle ArduCopter` generates this compact schema from ArduPilot's machine-readable `apm.pdef.json` endpoint rather than scraping raw HTML.
