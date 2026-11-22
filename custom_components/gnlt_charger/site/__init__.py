"""Site control core ported from evcc (pure Python, no Home Assistant imports).

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply).
Licence text: ``custom_components/gnlt_charger/modbus/NOTICE``.

Source: evcc master of, commit
82d37e922b2c0278d56eb90c5fdd3c0cd2c62175, ``core/`` (MIT). Nothing here comes
from evcc's ``charger/em2go*.go`` (not MIT).

Modules
-------
*:mod:`.api` - charge modes and status, the charger/meter/circuit protocols.
*:mod:`.clock` - injectable clock (tests use:class:`.clock.MockClock`).
*:mod:`.grid` - current <-> power (evcc ``util/grid``).
*:mod:`.loadpoint` - evcc ``core/loadpoint*.go``: modes off / now / smart
  (``pv`` and ``minpv`` are accepted and normalized like evcc), min/max
  current, surplus control with enable/disable thresholds and delays,
  automatic 1p/3p switching, charger sync, the 60 s settle windows.
*:mod:`.circuit` - evcc ``core/circuit/circuit.go``: hierarchical current
  and power limits (the load-management hook).
*:mod:`.prioritizer` - evcc ``core/prioritizer``: lower-priority
  loadpoints' flexible power is available to higher-priority ones.
*:mod:`.site` - evcc ``core/site.go`` / ``site_circuits.go``: site power
  from grid (+ PV, battery and aux power) meters, priorities, the loadpoint
  update loop (one loadpoint per cycle, round-robin, as evcc).

The modules above have no Home Assistant imports and talk to no station.
Home Assistant side (a "GNLT site" config entry over several ModBus entries):

*:mod:`.gnlt_charger_adapter` - the evcc charger interface of one station
  (Station / Commander, origin ``site``);
*:mod:`.coordinator` - one task per site: DLM allocation (:mod:`.dlm`), the
  per-member circuit cap, immediate decreases, the evcc site tick;
*:mod:`.config`,:mod:`.config_flow`,:mod:`.ha`,:mod:`.members`,
  mod:`.entities`,:mod:`.diagnostics` - settings, flow, set-up, membership,
  entities, diagnostics.

Excluded evcc features (not ported)
-----------------------------------
* vehicles: identification, SoC, vehicle min/max current and phases,
  limit SoC / limit energy, min SoC, charge plans and the planner,
  welcome charge, climater keep-alive, vehicle wake-up;
* tariffs and smart cost / smart feed-in priority limits, CO2, green share;
* battery strategy: priority/buffer/buffer-start SoC, battery boost, battery
  mode control and discharge limits (battery *power* still enters the site
  power, as in evcc);
* the optimizer and its suggestions;
* sessions, metrics, charge rater/timer, energy statistics;
* UI publishing and settings persistence (``publish`` is an optional
  callback only), messaging and push events;
* HEMS (consumption caps, PV curtailment), heating/integrated
  and switch-socket devices, excess DC power of hybrid inverters, Modbus
  retry/backoff around meter reads.
"""
