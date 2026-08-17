# War Thunder 8111 Bomb Alarm

**English** | [简体中文](./README.zh-CN.md)

A bombing-route and browser-notification assistant that only reads the local War Thunder telemetry interface at `127.0.0.1:8111`. It does not inject into the game, read game memory, or search for and display enemy units.

## Getting started

1. Start War Thunder and enter a test flight or battle.
2. Run the following command in PowerShell:

   ```powershell
   .\start.ps1
   ```

3. Open <http://127.0.0.1:8112> in your browser.
4. Select **Enable notifications**, then choose a `B` bombing zone, `A` airfield, or `Z` capture/defence zone on the map. You can also click any map position to set a custom target.

If a bombing zone disappears from the tactical map for at least 1.5 seconds, the application treats it as destroyed. If it was the selected target, the route is cleared automatically and a notification is displayed. When a new zone appears or a destroyed zone returns, the application reports that the zones have refreshed and resets the selected target.

The **Base Loadout Planner** reads BR ranges, base HP, bomb quantities, and nation loadout pages from the public [LEGION's Loadouts · Bomb Chart](https://docs.google.com/spreadsheets/d/1oNwp_MXszU5J2dcaz5IoCtSAQ-infPdOWhwtJXqtrwU/edit?gid=1447098598#gid=1447098598), caching the results for six hours. The application attempts to match the internal aircraft name supplied by 8111 to the aircraft, BR, and compatible loadouts listed in the chart. Because 8111 does not expose the currently selected bomb, the bomb type must still be selected manually when multiple loadouts are available.

To preview the interface without running the game:

```powershell
python .\wt_bomb_alert.py --demo
```

## 8111 endpoints

| Endpoint | Purpose |
| --- | --- |
| `/state` | True airspeed, altitude, and vertical speed |
| `/indicators` | Vehicle type, pitch, and roll indicators |
| `/map_info.json` | Converts normalized map coordinates to metres |
| `/map_obj.json` | Locates the player, tracks fixed mission-zone states, and filters confirmed blue/green allied aircraft only |
| `/map.img` | Built-in tactical-map background |

## Ballistic model

The current version uses a simplified two-dimensional model in which the bomb inherits the aircraft's velocity:

```text
t_fall = (v_vertical + sqrt(v_vertical² + 2·g·height)) / g
release_lead = horizontal_speed · t_fall · retention
time_to_release = (along_track_distance - release_lead) / closing_speed + calibration
```

Here, height is calculated as aircraft altitude minus target elevation. `retention` approximates the horizontal velocity retained by the bomb under drag.

## Important limitations

- The 8111 interface does not provide complete bomb mass, drag coefficients, wind, target terrain elevation, or weapon-specific ballistic tables. The default solution is only a release reminder and is not a replacement for in-game CCIP/CCRP.
- The Bomb Chart is community-maintained and its title still references game version 2.25. After game updates, BR changes, or bomb-damage adjustments, verify the result in a test flight and override the BR manually if necessary.
- Four-base maps use the chart's original values. The three-base “half load” and non-respawning Arcade-base “double load” options are rules of thumb from the chart FAQ. The planner rounds upward, but these estimates may not cover every mission rule.
- Different bombs, speeds, and altitudes should be calibrated in a test flight using the retention and time-offset controls.
- Tactical-map and telemetry updates are not frame-perfect. Allow a larger safety margin for high-speed, low-altitude releases.
- Browser notifications require the dashboard to remain open and permission to be granted manually.
- Avoid using 8111 data to expose enemy targets that would not normally be visible. Gaijin has stated on its official forum that doing so may be treated as ESP or an unfair advantage.
- The mission-zone layer uses a strict allowlist and never passes aircraft, tank, ship, anti-aircraft, or other combat-unit objects to the front end.
- The allied-aircraft layer only passes aircraft whose colour is clearly blue or green. Red and unknown-team aircraft are filtered by the back end.

## Tests

```powershell
python -m unittest discover -s tests -v
```
