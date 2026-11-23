# Aqara C1 pet feeder: on-device feeding schedule for ZHA

Store a feeding schedule **on the Aqara C1 smart pet feeder itself**
(`aqara.feeder.acn001`, ZNCWWSQ01LM) from Home Assistant's built-in Zigbee
integration (ZHA), and manage it from a dashboard.

Why: if Home Assistant, the Zigbee coordinator, Wi-Fi or mains power goes down,
automations that press "Feed" stop working. The C1 can run on backup batteries
and follows its own internal schedule. The stock ZHA quirk can't set that
schedule. Zigbee2MQTT can, and this project brings the same feature to ZHA.

## What's included

| Path | What it does |
|---|---|
| `custom_zha_quirks/aqara_feeder_acn001_schedule.py` | ZHA custom quirk. It is the stock C1 quirk plus a writable `feeding_schedule` attribute and a **Feeding schedule** sensor that shows the schedule the feeder reports back. All existing entities keep their IDs. |
| `packages/aqara_c1_feeder.yaml` | Control panel back end: 6 feeding slots (on/off, time, portions), feeding days, planned portions/grams per day, a "schedule sent" indicator and a **Send** script. |
| `dashboard/cat_feeder_dashboard.yaml` | Ready-made dashboard view using only built-in cards. |
| `tests/` | Tests against the real `zha`/`zha-quirks`/`zigpy` libraries. |

## Installation

1. **Quirk.** Copy `custom_zha_quirks/` to your config folder, so you have
   `/config/custom_zha_quirks/aqara_feeder_acn001_schedule.py`, and add to
   `configuration.yaml`:

   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks/
   ```

   Restart Home Assistant. The feeder gains a **Feeding schedule** sensor. It
   shows *unknown* until the first schedule is sent.

2. **Control panel (optional).** Enable packages in `configuration.yaml` and
   copy `packages/aqara_c1_feeder.yaml` to `/config/packages/`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

   The package assumes your feeder's entity IDs start with `pet_feeder_`
   (for example `select.pet_feeder_mode`). Either rename the device to
   *Pet feeder*, or search and replace `pet_feeder_` with your own prefix in
   the package and dashboard files. Restart again.

3. **Dashboard (optional).** *Settings → Dashboards → Add dashboard → New
   dashboard from scratch*. Open it, then *⋮ → Edit dashboard → ⋮ → Raw
   configuration editor*, and paste in `dashboard/cat_feeder_dashboard.yaml`.

## Usage

On the dashboard, switch feedings on, set their time and portions, pick the
days and press **Send**. The script writes the schedule to the feeder and puts
it in *Schedule* mode. Once the feeder confirms, *Feeder has this schedule*
turns on.

Without the panel, call the action yourself:

```yaml
action: zha.set_zigbee_cluster_attribute
data:
  ieee: "00:11:22:33:44:55:66:77"   # your feeder's IEEE address
  endpoint_id: 1
  cluster_id: 64704                 # 0xFCC0
  cluster_type: in
  attribute: 5011                   # 0x1393 feeding_schedule
  value: "07:00x2,11:00x2,15:00x2,19:00x2,23:00x2"
  manufacturer: 4447                # 0x115F
```

Remember to set the feeder's *Mode* to *Schedule* as well.

### Schedule format

```
[<days>@]HH:MMx<portions>[,...]
```

| Example | Meaning |
|---|---|
| `07:00x2,19:30x1` | every day, 2 portions at 07:00 and 1 at 19:30 |
| `workdays@06:30x2,weekend@08:00x2` | weekdays at 06:30, weekends at 08:00 |
| `mon+wed+fri@18:00x1` | specific days |

`<days>`: `everyday` (default), `workdays`, `weekend`, `mon` … `sun`,
`mon-wed-fri-sun`, `tue-thu-sat`, day names joined with `+`, or a bitmask 1–127
(mon=1 … sun=64). Portions per feeding: 1–10. Up to 10 feedings.

## How it works (protocol notes)

The C1 uses Aqara's manufacturer cluster `0xFCC0` (manufacturer code
`0x115F`). Every setting is tunnelled through attribute `0xFFF1` as an
octet string:

```
00 02 <seq>  <sub-attribute, 4 bytes BE>  <len>  <value>
```

The schedule is sub-attribute `0x080008C8`. Its value is ASCII: one hex string
per feeding, made of the 5 bytes `[days, hour, minute, portions, 0]`. Feedings
are joined with `,` and the whole string ends with a NUL byte:

```
"07:00x2,19:30x1"  ->  "7f07000200,7f131e0100\0"
```

This matches Zigbee2MQTT's `lumi_feeder` converter. Observed on a real C1
during development (not documented elsewhere as far as we know):

- Right after a schedule write, the feeder echoes the stored schedule back
  with two extra bytes (`05 a5`) before the sub-attribute id, in **uppercase**
  hex, without the NUL.
- Long echoes are **split over two reports**. The continuation frame starts
  with a byte that has bit `0x80` set (`80 02 02 …`) and carries the rest of
  the string.

The quirk handles both and only shows what the feeder reported.

## Tests

```sh
pip install -r tests/requirements.txt
pytest
```

Tested with Home Assistant 2026.11.2 (zha 2.4.3, zha-quirks 2.4.1,
zigpy 2.4.1) on a real C1 feeder.

## Credits & license

- Protocol details: Zigbee2MQTT / zigbee-herdsman-converters `lumi_feeder`.
- Base quirk: zigpy/zha-device-handlers `xiaomi/aqara/feeder_acn001.py`.

Apache-2.0, same license as zha-device-handlers. Ideally this should be
upstreamed into zha-device-handlers so a custom quirk is no longer needed.
