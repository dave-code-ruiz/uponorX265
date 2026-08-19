# Systembeskrivning – uponorX265

Home Assistant custom integration som ansluter till en **Uponor Smatrix Pulse**-gateway via lokalt nätverk och exponerar värme-/kylsystemets termostater, kontroller och gateway som HA-entiteter. All kommunikation sker lokalt (`iot_class: local_polling`) — inget moln involverat.

## Innehåll

1. [Arkitektur](#arkitektur)
2. [Funktionalitet](#funktionalitet)
3. [Officiella Pulse-appen (referens)](#officiella-pulse-appen-referens)
4. [Rådata (JNAP-variabler)](#rådata-jnap-variabler)
5. [Hårdvara som stöds](#hårdvara-som-stöds)

---

## Arkitektur

### Kommunikationslager — [jnap.py](custom_components/uponorx265/jnap.py)
`UponorJnap` pratar JNAP (JSON Network API) mot gatewayens `/JNAP/`-endpoint via `aiohttp`. Två operationer: `get_data()` (hämtar alla variabler som platt dict `waspVarName → waspVarValue`) och `send_data()` (skriver variabler). Inbyggd retry-logik (2 försök, 1s delay) och gemensam timeout; nätverksfel omvandlas till `HomeAssistantError`.

### Tillståndslager — [__init__.py](custom_components/uponorx265/__init__.py)
`UponorStateProxy` är den centrala klassen: håller rå-datat (`_data`) i minnet, pollar gatewayen på `SCAN_INTERVAL` (30s), och exponerar typade getters/setters (`get_setpoint`, `async_set_target_temperature`, `get_bypass_enable`, osv.) som platform-filerna bygger entiteter från. Vid varje uppdatering skickas `SIGNAL_UPONOR_STATE_UPDATE` via HA:s dispatcher så alla entiteter uppdaterar sig samtidigt. Data cachas även i `Store` (per config entry) för snabb återstart. Modulen registrerar även integrationens tjänster (`set_variable`, `dump_hardware_info`, `dump_raw_data`).

### Gateway-ID (MAC-uppslag) — [__init__.py](custom_components/uponorx265/__init__.py) / [helper.py](custom_components/uponorx265/helper.py)
Gatewayens `device_info`-identifierare och serienummer baseras på dess MAC-adress när den kan slås upp, annars faller den tillbaka på ett host-baserat ID (IP-adressen utan punkter). `UponorStateProxy.async_resolve_gateway_id()` körs i `async_setup_entry` innan plattformarna byggs (eftersom `device_info` läser `get_gateway_id()`), och försöker i tur och ordning:

1. `get_mac_address(ip=host)` direkt — fungerar om OS:ets ARP-cache redan har en post.
2. `_get_mac_with_arp_refresh()` i [helper.py](custom_components/uponorx265/helper.py) — primar ARP-cachen genom att faktiskt skicka data på en UDP-socket (inte bara `connect()`, som inte garanterat skickar något), försöker sedan `getmac` igen, och som sista utväg läser `/proc/net/arp` direkt (för HA-installationer i Docker där `arp`/`ip neighbor`-binärer kan saknas i containern).
3. MAC-adressen versaliseras (`.upper()`) innan den används som ID.

**Viktig begränsning:** ARP fungerar bara inom samma broadcast-domän/subnät. Om HA-värden och gatewayen ligger på olika subnät/VLAN kan MAC-adressen aldrig slås upp (kärnan får aldrig en ARP-post för den), och det faller permanent tillbaka på host-baserat ID — det är inte en bugg i koden, utan en nätverkstopologisk begränsning.

Eftersom gateway-ID:t kan ändra format över tid (host-baserat → lowercase-MAC → uppercase-MAC, i den ordningen integrationen har utvecklats), hanterar `_migrate_gateway_device_id()` övergången: hittar den gamla enhetsposten i device registry och byter namn på den identifierare in-place om ingen ny post finns, eller flyttar över area/anpassat namn och tar bort den gamla posten om en ny redan skapats av ett tidigare omstartsförsök. Detta körs vid varje uppstart och är idempotent.

### Entitetsbas — [helper.py](custom_components/uponorx265/helper.py)
Tre basklasser bygger en devicehierarki i HA:

| Basklass | Device | `via_device` |
|---|---|---|
| `UponorGatewayEntity` | Gateway (rot) | — |
| `UponorControllerEntity` | Reglercentral | Gateway |
| `UponorThermostatEntity` | Termostat | Reglercentral |

Alla ärver polling-fritt beteende (`should_poll = False`) och prenumererar på dispatcher-signalen för push-uppdatering.

### Platform-filer
En fil per HA-domän; var och en läser `hass.data[unique_id]` för state_proxy + listor över controllers/termostater och bygger entiteter:

| Fil | Innehåll |
|---|---|
| [climate.py](custom_components/uponorx265/climate.py) | Huvudentiteten per termostat (temperatur, HVAC-läge, presets: Comfort/Eco/Away/HA controlled) |
| [sensor.py](custom_components/uponorx265/sensor.py) | Temperatur, luftfuktighet, status, relakonfiguration, pumpstyrning m.m. |
| [binary_sensor.py](custom_components/uponorx265/binary_sensor.py) | Ventil, pumprelä, panntillslag (boiler demand), bypass (read-only) |
| [switch.py](custom_components/uponorx265/switch.py) | Away, Cool mode, HA-override (dial-termostater), auto-uppdatering, bypass (installatörsläge) |
| [select.py](custom_components/uponorx265/select.py) | Relakonfiguration och pumpstyrning (skrivbara, installatörsläge) |
| [config_flow.py](custom_components/uponorx265/config_flow.py) | Setup-wizard + options flow (host, controllernamn, rumsnamn, funktionsval) |

---

## Funktionalitet

**Grundfunktion:** varje fysisk termostat blir en `climate`-entitet med målsatt temperatur, aktuell temperatur/fukt, HVAC-läge (Heat/Cool + Off) och presets. Dial-termostater (T-144/T-145) kräver "HA controlled"-läge (local override) innan HA får styra börvärdet.

**Two-tier funktionsmodell**, styrd av flaggor i config entry:
- `controller_io` → skapar relä-/IO-sensorer per kontroller (pumprelä, panntillslag)
- `installer_settings` ("Installatörsläge") → gör relakonfiguration, bypass och pumpstyrning **skrivbara** (select/switch); annars visas motsvarande data som **read-only sensorer**. Samma `unique_id`-format delas mellan skriv-/läsversionen så historik bevaras vid växling.

**Affärsregler inbyggda i entiteterna:**
- Max 2 aktiva bypass-zoner per kontroller (enforced i `BypassEnableSwitch.async_turn_on`, kastar `HomeAssistantError` annars)
- Pumprelä döljs för C2–C4 när pumpstyrning är satt till "gemensam" (common)
- Bypass default är av

**Multi-gateway-stöd:** flera config entries kan köras parallellt; tjänster som `set_variable` och `dump_raw_data` matchar mot rätt gateway via `device_id`, eller mot den enda konfigurerade om bara en finns.

**Migrering:** `_migrate_entity_unique_ids` hanterar historiska unique_id-formatändringar (prefix-tillägg, climate-suffix) automatiskt vid uppstart så uppgraderingar inte skapar dubbletter.

---

## Officiella Pulse-appen (referens)

I Uponor Smatrix Pulse-appen (kräver kommunikationsmodul, se [R-208](#uponor-smatrix-pulse-com-r-208-kommunikationsmodul)) finns per termostat följande menyval:

- **Mina ECO profiler** — schemaläggning av Komfort/ECO-växling per rum
- **Visa trender** — historik över temperatur/luftfuktighet över tid
- **Rumsinställningar** — konfiguration av det enskilda rummets termostat

Dessa funktioner ligger i Uponors app/moln och har ingen motsvarighet i integrationen idag — JNAP-gatewayen exponerar inte ECO-profilscheman eller historiska trenddata, bara aktuella variabelvärden (se [dump_raw_data](custom_components/uponorx265/__init__.py)).

### Struktur under "Mina ECO profiler"

**System ECO-justering**
- Global temperaturförskjutning för ECO-läge, −4 °C till +4 °C.

**Förinställda profiler**
- 6 stycken (ECO-profil 1–6), var och en med:
  - Dagval (vilka veckodagar profilen gäller)
  - 3 tidsintervall per dag, vart och ett med ECO på/av-tidpunkt

**Mina ECO-profiler** (användardefinierade, per rum)
- Varje profil har: namn (redigerbart), tilldelat rum, schema Mån–Sön
- "Lägg till ECO" skapar en ny egen profil

Ingen av dessa scheman/profiler finns representerade som HA-variabler i det data integrationen läser via JNAP — de hanteras helt i appen/reglercentralens interna logik.

### Struktur under "Rumsinställningar"

- **Rumsnamn** → ändra rummets namn
- **ECO-profil** → tilldela en av "Mina ECO-profiler" (t.ex. "Min ECO-profil 1") till rummet
- **ECO temperatursänkning** → sätt temperatursänkningen för rummet, 0,5–10 °C
- **Åsidosätt termostatvärde** → på/av-reglage
- **Avancerade rumsinställningar** →
  - **Max börvärde** → 5–35 °C
  - **Min börvärde** → 5–35 °C
  - **Lägg till i medeltemperatur** → på/av; endast visningsvärde, påverkar inte driften (PÅ som standard)
  - **Komfortinställning** → 0–12 %, grundnivå för komfort när inget värmebehov finns (kortar uppvärmningstid, t.ex. vid annan värmekälla som braskamin — värdet är andel av tid styrdonen hålls öppna)
  - **Golvtemperatur** (visning) samt **Maximal/Lägsta golvtemperatur** (gränsvärden, endast vid RFT-regleringsläge)

Jämförelse mot integrationen:
- **Max/Min börvärde** motsvarar redan `get_max_limit()` / `get_min_limit()` i [climate.py](custom_components/uponorx265/climate.py) (`min_temp`/`max_temp`-properties).
- **ECO temperatursänkning** motsvarar `get_eco_setback()`, exponerad via `UponorClimate.extra_state_attributes`.
- **Lägg till i medeltemperatur** motsvarar `ClimatControlInAvg`-switchen (`avg_included`, styrs av `get_inavg()`/`async_iset_inavg()`) i [switch.py](custom_components/uponorx265/switch.py), gated bakom `CONF_SWITCH_SENSOR_AVG`.
- **ECO-profil, Rumsnamn, Åsidosätt termostatvärde, Komfortinställning, Golvtemperaturgränser** har ingen motsvarighet i integrationen idag — inte tillgängliga via JNAP-variablerna som läses in.

### Systeminställningar / Installatörsinställningar i appen

Nås via appens sidomeny → "Systeminställningar", eller specifikt "Installatörsinställningar" (varningstext: *"Om du ändrar dessa inställningar kan ditt system sluta fungera korrekt"*).

- **Kyla** → aktivera kylläge i systemet (avaktiverat vid leverans); ger sedan åtkomst till kylinställningar
- **GPI-konfiguration** → ställer in vilken signaltyp reglercentralens GPI (universalingång) tar emot: **Omkoppling Komfort/ECO** eller **Allmänt systemlarm** (Omkoppling värme/kyla kräver att systemet har värme/kyla; inaktiveras automatiskt om en extern Komfort/ECO-omkopplare, t.ex. en T-143 som systemenhet, redan är ansluten)
- **Pumpstyrning** → **Individuell** (en cirkulationspump per reglercentral, ansluten till relä 1) eller **Gemensam** (en pump för hela systemet, ansluten till masterreglercentralens relä 1 — reläerna på underreglercentraler blir då tillgängliga för andra funktioner)
- **Reläer för reglercentral** → två oberoende reläer (Relä 1 / Relä 2) per reglercentral, med fördefinierade kombinationer:
  - **Masterreglercentral:** Cirkulationspump+Panna (standard) · Cirkulationspump+Omkoppling värme/kyla · Cirkulationspump+Avfuktare · Kylaggregat+Panna · Cirkulationspump+Komfort/ECO · Ej konfigurerad+Ej konfigurerad
  - **Underreglercentral** (kräver kommunikationsmodul): Cirkulationspump+Omkoppling värme/kyla · Cirkulationspump+Avfuktare · Ej konfigurerad+Ej konfigurerad
- **Bypass rum** → systemet hanterar bypass för **upp till två rum per reglercentral** (för att upprätthålla minimiflöde); rum väljs manuellt per reglercentral-flik, eller med en tidsgräns för bypassfunktionen
- **Motionering av ventil/pump** → förhindrar att cirkulationspumpar/styrdon kärvar vid längre inaktivitet. Som standard: var 6:e dag ±24 h, pumpen körs 3 minuter, styrdonen öppnas/stängs helt. Körs fristående per komponent, endast om komponenten inte använts sedan senaste motioneringen
- **Autobalansering** → **Aktiverad** (standard) eller **Inaktiverad**; styr styrdonens utgångar via pulsbreddsmodulering (PWM) i stället för enkla till/från-signaler, ger jämnare golvtemperaturer, snabbare reaktionstid och lägre energiförbrukning. Kan kombineras med instrypt balansering
- **Installationens namn** → fritt textfält
- **Gräns för låg medeltemperatur** → utlöser larm om systemets medeltemperatur (beräknad från rum flaggade "Lägg till i medeltemperatur") faller under gränsvärdet. Förinställning 10 °C (5–30 °C), plus hysteres, förinställning 5 °C (1–10 °C); larmet släcks när medeltemperaturen stiger över gräns + hysteres
- **Systeminformation** → lista över alla anslutna enheter (reglercentral, kommunikationsmodul, termostater) med modell, mjukvaruversion och ID, samt möjlighet att trigga uppdatering
- **Tillopp temp. kontroll** → på/av-reglage (framledningstemperaturövervakning)

Jämförelse mot integrationen:
- **Pumpstyrning** (Individuell/Gemensam) motsvarar exakt `PumpManagementSelect`/`get_pump_management()`/`sys_pump_management` i [select.py](custom_components/uponorx265/select.py) och [__init__.py](custom_components/uponorx265/__init__.py) — samma `"0"`/`"1"`-värden.
- **Reläer för reglercentral** motsvarar `ControllerRelayConfigSelect`/`get_controller_relayconfig()` (`C?_controller_relays_config`) i samma filer; integrationens `RELAY_CONFIG_OPTIONS` (`not_in_use`/`pump_heater`/`pump_eco_comfort`/`not_configured`) är en förenklad delmängd av apptabellens kombinationer.
- **Bypass rum, max 2 per reglercentral** bekräftar exakt den affärsregel som redan är hårdkodad i `BypassEnableSwitch.async_turn_on` ([switch.py](custom_components/uponorx265/switch.py)) — appens gräns och integrationens gräns är alltså identiska.
- **Kyla, GPI-konfiguration, Motionering av ventil/pump, Autobalansering, Gräns för låg medeltemperatur, Installationens namn, Systeminformation, Tillopp temp. kontroll** har ingen motsvarighet i integrationen idag.

---

## Rådata (JNAP-variabler)

`dump_raw_data`-tjänsten returnerar hela `_data`-dictionaryn rakt av — alla `waspVarName`/`waspVarValue`-par gatewayen exponerar. Variabelnamnen följer några tydliga prefixmönster:

| Prefix | Nivå | Exempel | Innehåll |
|---|---|---|---|
| `cust_*` | Gateway/kund | `cust_Controller1_Name`, `cust_C1_T1_name`, `cust_wifi_device`, `cust_ip_device`, `cust_Enable_SW_Update`, `cust_General_RH_Setpoint`, `cust_Low_temperature_Limit` | Namn (reglercentraler, rum), nätverk, mjukvaruuppdatering, larmgränser |
| `sys_*` | System | `sys_pump_management`, `sys_autobalance`, `sys_heat_cool_mode`, `sys_time_limit_bypass`, `sys_day`/`sys_Month`/`sys_year`/…, `sys_controller_?_presence` | Globala driftinställningar, systemklocka, vilka reglercentraler som är anslutna |
| `C?_*` (utan `T?`) | Reglercentral | `C1_controller_relays_config`, `C1_stat_pump_relay`, `C1_stat_demand`, `C1_output_module_configuration`, `C1_general_purpose_input`, `C1_average_room_temperature`, `C1_sw_version` | Reläkonfiguration, pump/panna-status, GPI, medeltemperatur, mjukvaruversion, larm |
| `C?_T?_*` | Termostat | `C1_T1_setpoint`, `C1_T1_room_temperature`, `C1_T1_eco_setting`, `C1_T1_bypass_enable`, `C1_T1_eco_profile_number`, `C1_T1_stat_*_error` | Bör-/rumstemperatur, ECO-inställningar, bypass, felstatusar per termostat |
| `C?_T?_<Veckodag>` | Termostat, schema | `C1_T1_Monday` … `C1_T1_Sunday` | 12-tecken hex-bitmask per veckodag — schemat för ECO-profilen som är tilldelad rummet |
| `controller?_id`, `C?_thermostat?_id`, `C?_TTH_?_id` | Identiteter | `controller1_id`, `C1_thermostat1_id` | Hårdvaru-ID:n för reglercentraler, termostater och externa TTH-sensorer |

**Rättelse mot tidigare notering:** ECO-profilernas veckoschema är faktiskt tillgängligt i rådatat via `C?_T?_<Veckodag>`-bitmaskerna (t.ex. `C1_T1_Monday: c0ffffffffff`) tillsammans med `C?_T?_eco_profile_number` och `cust_C?_T?_Custom_Eco_Profile`. Det som saknas är inte datat i sig, utan en tolkning/exponering av det i integrationen — bitmaskformatet är inte avkodat någonstans i koden idag.

Andra observationer från dumpen:
- `sys_pump_management: '1'` (gemensam) och `C1_controller_relays_config: '3'` / `C2_controller_relays_config: '1'` visar `pump_heater` på C1 och `not_in_use` på C2 — matchar regeln att C2:s pumprelä döljs när pumpstyrningen är gemensam.
- `C1_stat_pump_relay` och `C1_stat_demand` är boolean-strängar (`'0'`/`'1'`), som förväntat av `get_pump_relay()`/`get_boiler_demand()`.
- Termperaturvärden (`setpoint`, `room_temperature`, m.fl.) lagras som heltal i tiondels grader (t.ex. `692` = 20,5 °C), `32767` betyder "ej ansluten/inget värde".
- `C1_general_purpose_input: '3'` och `C1_output_module_configuration: '7'` är separata bitfält från `controller_relays_config` — inte samma sak som relakonfigurationsvalet i appen.

<details>
<summary>Exempel på fullständig <code>dump_raw_data</code>-utdata (anonymiserad)</summary>

```yaml
cust_New_ControllerSW: '0'
cust_CX_SW_Distributed: '0'
cust_Start_SW_Update: '0'
cust_Update_Counter_TimeOut: '0'
cust_Update_SW_Retries: '0'
cust_SW_Update_Fail: '0'
cust_Mini_FW_Updated: '0'
cust_General_RH_Setpoint: '75'
cust_controller_1_lost: '0'
cust_Controller1_Name: nere
cust_wifi_device: ethernet
cust_ip_device: 10.x.x.x
cust_Enable_SW_Update: '1'
cust_C1_T1_name: Renee lekrum
cust_C1_T2_name: Hallen
cust_C1_T3_name: Tv rum
cust_C1_T4_name: Gammla Kontor
cust_C1_T5_name: Badrum
cust_C1_T6_name: Vardagsrum
cust_C1_T7_name: Köket
cust_Low_temperature_Limit: '500'
cust_Enable_Low_Temp_Alarm: '0'
cust_Low_temperature_Hyst: '90'
cust_SW_version_update: X245_122.hex
cust_Succesfull_SW_Instal: '1'
cust_C2_T1_name: Emmas gammla
cust_C2_T2_name: Sovrum R&F
cust_Controller2_Name: uppe
cust_C2_T3_name: Sovrum olivia
cust_C2_T4_name: Allrum
cust_C2_T5_name: Kontor
cust_C2_T6_name: Badrum uppe
sys_valve_exercise: '0'
sys_pump_exercise: '0'
sys_supply_diagnostic: '0'
sys_autobalance: '1'
sys_pump_management: '1'
sys_rh_control_activation: '0'
sys_supply_water_activation: '0'
sys_cooling_available: '0'
sys_forced_eco_mode: '0'
sys_heat_cool_mode: '0'
sys_comm_module_exist: '1'
sys_time_limit_bypass: '0'
sys_heat_pump_dynamic_heatcurve: '0'
sys_heat_pump_response: '0'
sys_heat_pump_defrost: '0'
sys_heat_cool_master_switch: '0'
Sys_CeilingCooling_Type: '0'
sys_HC_supply_limit: '644'
sys_HC_supply_hyst: '72'
sys_first_stage_offset: '36'
sys_day: '1'
sys_Month: '8'
sys_year: '26'
sys_minutes: '29'
sys_hour: '20'
sys_days: '18'
sys_seconds: '5'
Sys_ext_outdoor_temp: '32767'
sys_heat_cool_offset: '36'
sys_eco_mode_offset: '72'
sys_indoor_temp_switch: '788'
sys_outdoor_temp_hyst: '36'
sys_outdoor_temp_switch: '824'
sys_indoor_temp_hyst: '72'
sys_indoor_temp_delay: '24'
sys_pun_protocol_version: '0'
sys_OTA_status: '0'
sys_controller_1_presence: '1'
sys_controller_2_presence: '1'
sys_controller_3_presence: '0'
sys_controller_4_presence: '0'
sys_controller_1_lost: '0'
sys_controller_2_lost: '0'
sys_controller_3_lost: '0'
sys_controller_4_lost: '0'
sys_average_relative_humidity: '0'
C1_channel_1_fancoil: '0'
C2_channel_1_fancoil: '0'
C1_channel_2_fancoil: '0'
C2_channel_2_fancoil: '0'
C1_channel_3_fancoil: '0'
C2_channel_3_fancoil: '0'
C1_channel_4_fancoil: '0'
C2_channel_4_fancoil: '0'
C1_channel_5_fancoil: '0'
C2_channel_5_fancoil: '0'
C1_channel_6_fancoil: '0'
C2_channel_6_fancoil: '0'
C1_channel_7_fancoil: '0'
C2_channel_7_fancoil: '0'
C1_channel_8_fancoil: '0'
C2_channel_8_fancoil: '0'
C1_channel_9_fancoil: '0'
C2_channel_9_fancoil: '0'
C1_channel_10_fancoil: '0'
C2_channel_10_fancoil: '0'
C1_channel_11_fancoil: '0'
C2_channel_11_fancoil: '0'
C1_channel_12_fancoil: '0'
C2_channel_12_fancoil: '0'
C1_out_relay_heat_cool_SwFunct: '0'
C2_out_relay_heat_cool_SwFunct: '0'
C1_general_purpose_input: '3'
C2_general_purpose_input: '3'
C1_output_module_configuration: '7'
C2_output_module_configuration: '7'
C1_controller_relays_config: '3'
C2_controller_relays_config: '1'
C1_channel_1_ceiling_cooling: '0'
C2_channel_1_ceiling_cooling: '0'
C1_channel_2_ceiling_cooling: '0'
C2_channel_2_ceiling_cooling: '0'
C1_channel_3_ceiling_cooling: '0'
C2_channel_3_ceiling_cooling: '0'
C1_channel_4_ceiling_cooling: '0'
C2_channel_4_ceiling_cooling: '0'
C1_channel_5_ceiling_cooling: '0'
C2_channel_5_ceiling_cooling: '0'
C1_channel_6_ceiling_cooling: '0'
C2_channel_6_ceiling_cooling: '0'
C1_channel_7_ceiling_cooling: '0'
C2_channel_7_ceiling_cooling: '0'
C1_channel_8_ceiling_cooling: '0'
C2_channel_8_ceiling_cooling: '0'
C1_channel_9_ceiling_cooling: '0'
C2_channel_9_ceiling_cooling: '0'
C1_channel_10_ceiling_cooling: '0'
C2_channel_10_ceiling_cooling: '0'
C1_channel_11_ceiling_cooling: '0'
C2_channel_11_ceiling_cooling: '0'
C1_channel_12_ceiling_cooling: '0'
C2_channel_12_ceiling_cooling: '0'
C1_channel_1_ave_temp: '0'
C2_channel_1_ave_temp: '1'
C1_channel_2_ave_temp: '0'
C2_channel_2_ave_temp: '1'
C1_channel_3_ave_temp: '1'
C2_channel_3_ave_temp: '1'
C1_channel_4_ave_temp: '1'
C2_channel_4_ave_temp: '1'
C1_channel_5_ave_temp: '0'
C2_channel_5_ave_temp: '1'
C1_channel_6_ave_temp: '0'
C2_channel_6_ave_temp: '0'
C1_channel_7_ave_temp: '0'
C2_channel_7_ave_temp: '1'
C1_channel_8_ave_temp: '1'
C2_channel_8_ave_temp: '1'
C1_channel_9_ave_temp: '1'
C2_channel_9_ave_temp: '1'
C1_channel_10_ave_temp: '1'
C2_channel_10_ave_temp: '1'
C1_channel_11_ave_temp: '1'
C2_channel_11_ave_temp: '1'
C1_channel_12_ave_temp: '1'
C2_channel_12_ave_temp: '1'
C1_rh_dead_zone: '5'
C2_rh_dead_zone: '5'
C1_rh_worst: '0'
C2_rh_worst: '0'
C1_sw_version: '290'
C2_sw_version: '290'
C1_thermostat_1_presence: '1'
C2_thermostat_1_presence: '1'
C1_thermostat_2_presence: '1'
C2_thermostat_2_presence: '1'
C1_thermostat_3_presence: '1'
C2_thermostat_3_presence: '1'
C1_thermostat_4_presence: '1'
C2_thermostat_4_presence: '1'
C1_thermostat_5_presence: '1'
C2_thermostat_5_presence: '1'
C1_thermostat_6_presence: '1'
C2_thermostat_6_presence: '1'
C1_thermostat_7_presence: '1'
C2_thermostat_7_presence: '0'
C1_thermostat_8_presence: '0'
C2_thermostat_8_presence: '0'
C1_thermostat_9_presence: '0'
C2_thermostat_9_presence: '0'
C1_thermostat_10_presence: '0'
C2_thermostat_10_presence: '0'
C1_thermostat_11_presence: '0'
C2_thermostat_11_presence: '0'
C1_thermostat_12_presence: '0'
C2_thermostat_12_presence: '0'
C1_output_module_presence: '0'
C2_output_module_presence: '0'
C1_outdoor_temp_sensor_presence: '0'
C2_outdoor_temp_sensor_presence: '0'
C1_heat_cool_presence: '0'
C2_heat_cool_presence: '0'
C1_eco_mode_presence: '0'
C2_eco_mode_presence: '0'
C1_stat_pump_relay: '0'
C2_stat_pump_relay: '0'
C1_stat_supply_temp_hi_alarm: '0'
C2_stat_supply_temp_hi_alarm: '0'
C1_stat_supply_temp_low_alarm: '0'
C2_stat_supply_temp_low_alarm: '0'
C1_eco_mode_forced_pub_thermo: '0'
C2_eco_mode_forced_pub_thermo: '0'
C1_stat_demand: '0'
C2_stat_demand: '0'
C1_stat_general_system_alarm: '0'
C2_stat_general_system_alarm: '0'
C1_device_system_alarm_eco_loss: '0'
C2_device_system_alarm_eco_loss: '0'
C1_stat_heat_cool_mode: '0'
C2_stat_heat_cool_mode: '0'
C1_stat_heat_cool_slave_input: '0'
C2_stat_heat_cool_slave_input: '0'
C1_thermostat_change_1: '0'
C2_thermostat_change_1: '0'
C1_thermostat_change_2: '0'
C2_thermostat_change_2: '0'
C1_thermostat_change_3: '0'
C2_thermostat_change_3: '0'
C1_thermostat_change_4: '0'
C2_thermostat_change_4: '0'
C1_thermostat_change_5: '0'
C2_thermostat_change_5: '0'
C1_thermostat_change_6: '0'
C2_thermostat_change_6: '0'
C1_thermostat_change_7: '0'
C2_thermostat_change_7: '0'
C1_thermostat_change_8: '0'
C2_thermostat_change_8: '0'
C1_thermostat_change_9: '0'
C2_thermostat_change_9: '0'
C1_thermostat_change_10: '0'
C2_thermostat_change_10: '0'
C1_thermostat_change_11: '0'
C2_thermostat_change_11: '0'
C1_thermostat_change_12: '0'
C2_thermostat_change_12: '0'
C1_average_room_temperature: '740'
C2_average_room_temperature: '762'
C1_average_setpoint: '32767'
C2_average_setpoint: '32767'
C1_outdoor_temperature: '32767'
C2_outdoor_temperature: '32767'
C1_alarm_type: '0'
C2_alarm_type: '0'
C1_supply_temperature: '32767'
C2_supply_temperature: '32767'
C1_worst_room_temperature: '32767'
C2_worst_room_temperature: '32767'
C1_worst_setpoint: '32767'
C2_worst_setpoint: '32767'
C1_stat_heat_pump_dyn_heat: '0'
C2_stat_heat_pump_dyn_heat: '0'
C1_hardware_type: '0'
C2_hardware_type: '0'
C1_memory_map: '1'
C2_memory_map: '1'
C1_out_module_relay1_cmd: '0'
C2_out_module_relay1_cmd: '0'
C1_out_module_relay2_cmd: '0'
C2_out_module_relay2_cmd: '0'
C1_stat_out_module_relay1: '0'
C2_stat_out_module_relay1: '0'
C1_stat_out_module_relay2: '0'
C2_stat_out_module_relay2: '0'
C1_stat_out_module_com_lost: '0'
C2_stat_out_module_com_lost: '0'
C1_pending_sw_version: '65535'
C2_pending_sw_version: '65535'
C1_bootloader_sw_version: '1044'
C2_bootloader_sw_version: '1044'
C1_T1_bypass_enable: '0'
C1_T2_bypass_enable: '0'
C1_T3_bypass_enable: '0'
C1_T4_bypass_enable: '0'
C1_T5_bypass_enable: '0'
C1_T6_bypass_enable: '0'
C1_T7_bypass_enable: '0'
C2_T1_bypass_enable: '0'
C2_T2_bypass_enable: '0'
C2_T3_bypass_enable: '0'
C2_T4_bypass_enable: '0'
C2_T5_bypass_enable: '0'
C2_T6_bypass_enable: '0'
C1_T1_manual_fan_on: '0'
C1_T2_manual_fan_on: '0'
C1_T3_manual_fan_on: '0'
C1_T4_manual_fan_on: '0'
C1_T5_manual_fan_on: '0'
C1_T6_manual_fan_on: '0'
C1_T7_manual_fan_on: '0'
C2_T1_manual_fan_on: '0'
C2_T2_manual_fan_on: '0'
C2_T3_manual_fan_on: '0'
C2_T4_manual_fan_on: '0'
C2_T5_manual_fan_on: '0'
C2_T6_manual_fan_on: '0'
C1_T1_mode_comfort_eco: '0'
C1_T2_mode_comfort_eco: '0'
C1_T3_mode_comfort_eco: '0'
C1_T4_mode_comfort_eco: '0'
C1_T5_mode_comfort_eco: '0'
C1_T6_mode_comfort_eco: '0'
C1_T7_mode_comfort_eco: '0'
C2_T1_mode_comfort_eco: '0'
C2_T2_mode_comfort_eco: '0'
C2_T3_mode_comfort_eco: '0'
C2_T4_mode_comfort_eco: '0'
C2_T5_mode_comfort_eco: '0'
C2_T6_mode_comfort_eco: '0'
C1_T1_dehumidifier_activation: '0'
C1_T2_dehumidifier_activation: '0'
C1_T3_dehumidifier_activation: '0'
C1_T4_dehumidifier_activation: '0'
C1_T5_dehumidifier_activation: '0'
C1_T6_dehumidifier_activation: '0'
C1_T7_dehumidifier_activation: '0'
C2_T1_dehumidifier_activation: '0'
C2_T2_dehumidifier_activation: '0'
C2_T3_dehumidifier_activation: '0'
C2_T4_dehumidifier_activation: '0'
C2_T5_dehumidifier_activation: '0'
C2_T6_dehumidifier_activation: '0'
C1_T1_rh_control: '0'
C1_T2_rh_control: '0'
C1_T3_rh_control: '0'
C1_T4_rh_control: '0'
C1_T5_rh_control: '0'
C1_T6_rh_control: '0'
C1_T7_rh_control: '0'
C2_T1_rh_control: '0'
C2_T2_rh_control: '0'
C2_T3_rh_control: '0'
C2_T4_rh_control: '0'
C2_T5_rh_control: '0'
C2_T6_rh_control: '0'
C1_T1_eco_profile_number: '7'
C1_T2_eco_profile_number: '0'
C1_T3_eco_profile_number: '0'
C1_T4_eco_profile_number: '0'
C1_T5_eco_profile_number: '0'
C1_T6_eco_profile_number: '0'
C1_T7_eco_profile_number: '0'
C2_T1_eco_profile_number: '0'
C2_T2_eco_profile_number: '0'
C2_T3_eco_profile_number: '0'
C2_T4_eco_profile_number: '0'
C2_T5_eco_profile_number: '0'
C2_T6_eco_profile_number: '0'
C1_T1_pub_setpoint_override: '1'
C1_T2_pub_setpoint_override: '1'
C1_T3_pub_setpoint_override: '1'
C1_T4_pub_setpoint_override: '1'
C1_T5_pub_setpoint_override: '1'
C1_T6_pub_setpoint_override: '1'
C1_T7_pub_setpoint_override: '1'
C2_T1_pub_setpoint_override: '1'
C2_T2_pub_setpoint_override: '1'
C2_T3_pub_setpoint_override: '1'
C2_T4_pub_setpoint_override: '1'
C2_T5_pub_setpoint_override: '1'
C2_T6_pub_setpoint_override: '1'
C1_T1_cooling_allowed: '1'
C1_T2_cooling_allowed: '1'
C1_T3_cooling_allowed: '1'
C1_T4_cooling_allowed: '1'
C1_T5_cooling_allowed: '1'
C1_T6_cooling_allowed: '1'
C1_T7_cooling_allowed: '1'
C2_T1_cooling_allowed: '1'
C2_T2_cooling_allowed: '1'
C2_T3_cooling_allowed: '1'
C2_T4_cooling_allowed: '1'
C2_T5_cooling_allowed: '1'
C2_T6_cooling_allowed: '1'
C1_T1_rh_setpoint: '75'
C1_T2_rh_setpoint: '75'
C1_T3_rh_setpoint: '75'
C1_T4_rh_setpoint: '75'
C1_T5_rh_setpoint: '75'
C1_T6_rh_setpoint: '75'
C1_T7_rh_setpoint: '75'
C2_T1_rh_setpoint: '75'
C2_T2_rh_setpoint: '75'
C2_T3_rh_setpoint: '75'
C2_T4_rh_setpoint: '75'
C2_T5_rh_setpoint: '75'
C2_T6_rh_setpoint: '75'
C1_T1_comfort_heating_setpoint: '0'
C1_T2_comfort_heating_setpoint: '0'
C1_T3_comfort_heating_setpoint: '0'
C1_T4_comfort_heating_setpoint: '0'
C1_T5_comfort_heating_setpoint: '0'
C1_T6_comfort_heating_setpoint: '0'
C1_T7_comfort_heating_setpoint: '8'
C2_T1_comfort_heating_setpoint: '0'
C2_T2_comfort_heating_setpoint: '0'
C2_T3_comfort_heating_setpoint: '0'
C2_T4_comfort_heating_setpoint: '0'
C2_T5_comfort_heating_setpoint: '0'
C2_T6_comfort_heating_setpoint: '0'
C1_T1_minimum_setpoint: '410'
C1_T2_minimum_setpoint: '410'
C1_T3_minimum_setpoint: '410'
C1_T4_minimum_setpoint: '410'
C1_T5_minimum_setpoint: '410'
C1_T6_minimum_setpoint: '410'
C1_T7_minimum_setpoint: '410'
C2_T1_minimum_setpoint: '410'
C2_T2_minimum_setpoint: '410'
C2_T3_minimum_setpoint: '410'
C2_T4_minimum_setpoint: '410'
C2_T5_minimum_setpoint: '410'
C2_T6_minimum_setpoint: '410'
C1_T1_maximum_setpoint: '950'
C1_T2_maximum_setpoint: '950'
C1_T3_maximum_setpoint: '950'
C1_T4_maximum_setpoint: '950'
C1_T5_maximum_setpoint: '950'
C1_T6_maximum_setpoint: '950'
C1_T7_maximum_setpoint: '950'
C2_T1_maximum_setpoint: '950'
C2_T2_maximum_setpoint: '950'
C2_T3_maximum_setpoint: '950'
C2_T4_maximum_setpoint: '950'
C2_T5_maximum_setpoint: '950'
C2_T6_maximum_setpoint: '950'
C1_T1_minimum_floor_setpoint: '680'
C1_T2_minimum_floor_setpoint: '680'
C1_T3_minimum_floor_setpoint: '680'
C1_T4_minimum_floor_setpoint: '680'
C1_T5_minimum_floor_setpoint: '680'
C1_T6_minimum_floor_setpoint: '680'
C1_T7_minimum_floor_setpoint: '680'
C2_T1_minimum_floor_setpoint: '680'
C2_T2_minimum_floor_setpoint: '680'
C2_T3_minimum_floor_setpoint: '680'
C2_T4_minimum_floor_setpoint: '680'
C2_T5_minimum_floor_setpoint: '680'
C2_T6_minimum_floor_setpoint: '680'
C1_T1_maximum_floor_setpoint: '788'
C1_T2_maximum_floor_setpoint: '788'
C1_T3_maximum_floor_setpoint: '788'
C1_T4_maximum_floor_setpoint: '788'
C1_T5_maximum_floor_setpoint: '788'
C1_T6_maximum_floor_setpoint: '788'
C1_T7_maximum_floor_setpoint: '788'
C2_T1_maximum_floor_setpoint: '788'
C2_T2_maximum_floor_setpoint: '788'
C2_T3_maximum_floor_setpoint: '788'
C2_T4_maximum_floor_setpoint: '788'
C2_T5_maximum_floor_setpoint: '788'
C2_T6_maximum_floor_setpoint: '788'
C1_T1_setpoint: '692'
C1_T2_setpoint: '696'
C1_T3_setpoint: '683'
C1_T4_setpoint: '687'
C1_T5_setpoint: '698'
C1_T6_setpoint: '687'
C1_T7_setpoint: '644'
C2_T1_setpoint: '667'
C2_T2_setpoint: '638'
C2_T3_setpoint: '644'
C2_T4_setpoint: '719'
C2_T5_setpoint: '698'
C2_T6_setpoint: '698'
C1_T1_eco_offset: '72'
C1_T2_eco_offset: '72'
C1_T3_eco_offset: '72'
C1_T4_eco_offset: '72'
C1_T5_eco_offset: '72'
C1_T6_eco_offset: '72'
C1_T7_eco_offset: '72'
C2_T1_eco_offset: '72'
C2_T2_eco_offset: '72'
C2_T3_eco_offset: '72'
C2_T4_eco_offset: '72'
C2_T5_eco_offset: '72'
C2_T6_eco_offset: '72'
C1_T1_stat_cb_wifi_installed: '1'
C1_T2_stat_cb_wifi_installed: '1'
C1_T3_stat_cb_wifi_installed: '1'
C1_T4_stat_cb_wifi_installed: '1'
C1_T5_stat_cb_wifi_installed: '1'
C1_T6_stat_cb_wifi_installed: '1'
C1_T7_stat_cb_wifi_installed: '1'
C2_T1_stat_cb_wifi_installed: '1'
C2_T2_stat_cb_wifi_installed: '1'
C2_T3_stat_cb_wifi_installed: '1'
C2_T4_stat_cb_wifi_installed: '1'
C2_T5_stat_cb_wifi_installed: '1'
C2_T6_stat_cb_wifi_installed: '1'
C1_T1_stat_cb_need_date_info: '0'
C1_T2_stat_cb_need_date_info: '0'
C1_T3_stat_cb_need_date_info: '0'
C1_T4_stat_cb_need_date_info: '0'
C1_T5_stat_cb_need_date_info: '0'
C1_T6_stat_cb_need_date_info: '0'
C1_T7_stat_cb_need_date_info: '0'
C2_T1_stat_cb_need_date_info: '0'
C2_T2_stat_cb_need_date_info: '0'
C2_T3_stat_cb_need_date_info: '0'
C2_T4_stat_cb_need_date_info: '0'
C2_T5_stat_cb_need_date_info: '0'
C2_T6_stat_cb_need_date_info: '0'
C1_T1_stat_cb_comfort_eco_mode: '0'
C1_T2_stat_cb_comfort_eco_mode: '0'
C1_T3_stat_cb_comfort_eco_mode: '0'
C1_T4_stat_cb_comfort_eco_mode: '0'
C1_T5_stat_cb_comfort_eco_mode: '0'
C1_T6_stat_cb_comfort_eco_mode: '0'
C1_T7_stat_cb_comfort_eco_mode: '0'
C2_T1_stat_cb_comfort_eco_mode: '0'
C2_T2_stat_cb_comfort_eco_mode: '0'
C2_T3_stat_cb_comfort_eco_mode: '0'
C2_T4_stat_cb_comfort_eco_mode: '0'
C2_T5_stat_cb_comfort_eco_mode: '0'
C2_T6_stat_cb_comfort_eco_mode: '0'
C1_T1_stat_cb_eco_forced: '0'
C1_T2_stat_cb_eco_forced: '0'
C1_T3_stat_cb_eco_forced: '0'
C1_T4_stat_cb_eco_forced: '0'
C1_T5_stat_cb_eco_forced: '0'
C1_T6_stat_cb_eco_forced: '0'
C1_T7_stat_cb_eco_forced: '0'
C2_T1_stat_cb_eco_forced: '0'
C2_T2_stat_cb_eco_forced: '0'
C2_T3_stat_cb_eco_forced: '0'
C2_T4_stat_cb_eco_forced: '0'
C2_T5_stat_cb_eco_forced: '0'
C2_T6_stat_cb_eco_forced: '0'
C1_T1_stat_cb_sub_actuator: '0'
C1_T2_stat_cb_sub_actuator: '0'
C1_T3_stat_cb_sub_actuator: '0'
C1_T4_stat_cb_sub_actuator: '0'
C1_T5_stat_cb_sub_actuator: '0'
C1_T6_stat_cb_sub_actuator: '0'
C1_T7_stat_cb_sub_actuator: '0'
C2_T1_stat_cb_sub_actuator: '0'
C2_T2_stat_cb_sub_actuator: '0'
C2_T3_stat_cb_sub_actuator: '0'
C2_T4_stat_cb_sub_actuator: '0'
C2_T5_stat_cb_sub_actuator: '0'
C2_T6_stat_cb_sub_actuator: '0'
C1_T1_stat_cb_actuator: '0'
C1_T2_stat_cb_actuator: '0'
C1_T3_stat_cb_actuator: '0'
C1_T4_stat_cb_actuator: '0'
C1_T5_stat_cb_actuator: '0'
C1_T6_stat_cb_actuator: '0'
C1_T7_stat_cb_actuator: '0'
C2_T1_stat_cb_actuator: '0'
C2_T2_stat_cb_actuator: '0'
C2_T3_stat_cb_actuator: '0'
C2_T4_stat_cb_actuator: '0'
C2_T5_stat_cb_actuator: '0'
C2_T6_stat_cb_actuator: '0'
C1_T1_stat_cb_rh_cool_shutdown: '0'
C1_T2_stat_cb_rh_cool_shutdown: '0'
C1_T3_stat_cb_rh_cool_shutdown: '0'
C1_T4_stat_cb_rh_cool_shutdown: '0'
C1_T5_stat_cb_rh_cool_shutdown: '0'
C1_T6_stat_cb_rh_cool_shutdown: '0'
C1_T7_stat_cb_rh_cool_shutdown: '0'
C2_T1_stat_cb_rh_cool_shutdown: '0'
C2_T2_stat_cb_rh_cool_shutdown: '0'
C2_T3_stat_cb_rh_cool_shutdown: '0'
C2_T4_stat_cb_rh_cool_shutdown: '0'
C2_T5_stat_cb_rh_cool_shutdown: '0'
C2_T6_stat_cb_rh_cool_shutdown: '0'
C1_T1_stat_cb_floor_limit_reach: '0'
C1_T2_stat_cb_floor_limit_reach: '0'
C1_T3_stat_cb_floor_limit_reach: '0'
C1_T4_stat_cb_floor_limit_reach: '0'
C1_T5_stat_cb_floor_limit_reach: '0'
C1_T6_stat_cb_floor_limit_reach: '0'
C1_T7_stat_cb_floor_limit_reach: '0'
C2_T1_stat_cb_floor_limit_reach: '0'
C2_T2_stat_cb_floor_limit_reach: '0'
C2_T3_stat_cb_floor_limit_reach: '0'
C2_T4_stat_cb_floor_limit_reach: '0'
C2_T5_stat_cb_floor_limit_reach: '0'
C2_T6_stat_cb_floor_limit_reach: '0'
C1_T1_stat_cb_fallbk_heatalarm: '0'
C1_T2_stat_cb_fallbk_heatalarm: '0'
C1_T3_stat_cb_fallbk_heatalarm: '0'
C1_T4_stat_cb_fallbk_heatalarm: '0'
C1_T5_stat_cb_fallbk_heatalarm: '0'
C1_T6_stat_cb_fallbk_heatalarm: '0'
C1_T7_stat_cb_fallbk_heatalarm: '0'
C2_T1_stat_cb_fallbk_heatalarm: '0'
C2_T2_stat_cb_fallbk_heatalarm: '0'
C2_T3_stat_cb_fallbk_heatalarm: '0'
C2_T4_stat_cb_fallbk_heatalarm: '0'
C2_T5_stat_cb_fallbk_heatalarm: '0'
C2_T6_stat_cb_fallbk_heatalarm: '0'
C1_T1_stat_cb_holiday_mode: '0'
C1_T2_stat_cb_holiday_mode: '0'
C1_T3_stat_cb_holiday_mode: '0'
C1_T4_stat_cb_holiday_mode: '0'
C1_T5_stat_cb_holiday_mode: '0'
C1_T6_stat_cb_holiday_mode: '0'
C1_T7_stat_cb_holiday_mode: '0'
C2_T1_stat_cb_holiday_mode: '0'
C2_T2_stat_cb_holiday_mode: '0'
C2_T3_stat_cb_holiday_mode: '0'
C2_T4_stat_cb_holiday_mode: '0'
C2_T5_stat_cb_holiday_mode: '0'
C2_T6_stat_cb_holiday_mode: '0'
C1_T1_stat_cb_heat_cool_mode: '0'
C1_T2_stat_cb_heat_cool_mode: '0'
C1_T3_stat_cb_heat_cool_mode: '0'
C1_T4_stat_cb_heat_cool_mode: '0'
C1_T5_stat_cb_heat_cool_mode: '0'
C1_T6_stat_cb_heat_cool_mode: '0'
C1_T7_stat_cb_heat_cool_mode: '0'
C2_T1_stat_cb_heat_cool_mode: '0'
C2_T2_stat_cb_heat_cool_mode: '0'
C2_T3_stat_cb_heat_cool_mode: '0'
C2_T4_stat_cb_heat_cool_mode: '0'
C2_T5_stat_cb_heat_cool_mode: '0'
C2_T6_stat_cb_heat_cool_mode: '0'
C1_T1_stat_air_sensor_error: '0'
C1_T2_stat_air_sensor_error: '0'
C1_T3_stat_air_sensor_error: '0'
C1_T4_stat_air_sensor_error: '0'
C1_T5_stat_air_sensor_error: '0'
C1_T6_stat_air_sensor_error: '0'
C1_T7_stat_air_sensor_error: '0'
C2_T1_stat_air_sensor_error: '0'
C2_T2_stat_air_sensor_error: '0'
C2_T3_stat_air_sensor_error: '0'
C2_T4_stat_air_sensor_error: '0'
C2_T5_stat_air_sensor_error: '0'
C2_T6_stat_air_sensor_error: '0'
C1_T1_stat_external_sensor_err: '0'
C1_T2_stat_external_sensor_err: '0'
C1_T3_stat_external_sensor_err: '0'
C1_T4_stat_external_sensor_err: '0'
C1_T5_stat_external_sensor_err: '0'
C1_T6_stat_external_sensor_err: '0'
C1_T7_stat_external_sensor_err: '0'
C2_T1_stat_external_sensor_err: '0'
C2_T2_stat_external_sensor_err: '0'
C2_T3_stat_external_sensor_err: '0'
C2_T4_stat_external_sensor_err: '0'
C2_T5_stat_external_sensor_err: '0'
C2_T6_stat_external_sensor_err: '0'
C1_T1_stat_rh_sensor_error: '0'
C1_T2_stat_rh_sensor_error: '0'
C1_T3_stat_rh_sensor_error: '0'
C1_T4_stat_rh_sensor_error: '0'
C1_T5_stat_rh_sensor_error: '0'
C1_T6_stat_rh_sensor_error: '0'
C2_T1_stat_rh_sensor_error: '0'
C2_T2_stat_rh_sensor_error: '0'
C2_T3_stat_rh_sensor_error: '0'
C2_T4_stat_rh_sensor_error: '0'
C2_T5_stat_rh_sensor_error: '0'
C2_T6_stat_rh_sensor_error: '0'
C1_T1_stat_comfort_eco_mode: '0'
C1_T2_stat_comfort_eco_mode: '0'
C1_T3_stat_comfort_eco_mode: '0'
C1_T4_stat_comfort_eco_mode: '0'
C1_T5_stat_comfort_eco_mode: '0'
C1_T6_stat_comfort_eco_mode: '0'
C1_T7_stat_comfort_eco_mode: '0'
C2_T1_stat_comfort_eco_mode: '0'
C2_T2_stat_comfort_eco_mode: '0'
C2_T3_stat_comfort_eco_mode: '0'
C2_T4_stat_comfort_eco_mode: '0'
C2_T5_stat_comfort_eco_mode: '0'
C2_T6_stat_comfort_eco_mode: '0'
C1_T1_stat_tamper_alarm: '0'
C1_T2_stat_tamper_alarm: '0'
C1_T3_stat_tamper_alarm: '0'
C1_T4_stat_tamper_alarm: '0'
C1_T5_stat_tamper_alarm: '0'
C1_T6_stat_tamper_alarm: '0'
C1_T7_stat_tamper_alarm: '0'
C2_T1_stat_tamper_alarm: '0'
C2_T2_stat_tamper_alarm: '0'
C2_T3_stat_tamper_alarm: '0'
C2_T4_stat_tamper_alarm: '0'
C2_T5_stat_tamper_alarm: '0'
C2_T6_stat_tamper_alarm: '0'
C1_T1_stat_rf_error: '0'
C1_T2_stat_rf_error: '0'
C1_T3_stat_rf_error: '0'
C1_T4_stat_rf_error: '0'
C1_T5_stat_rf_error: '0'
C1_T6_stat_rf_error: '0'
C1_T7_stat_rf_error: '0'
C2_T1_stat_rf_error: '0'
C2_T2_stat_rf_error: '0'
C2_T3_stat_rf_error: '0'
C2_T4_stat_rf_error: '0'
C2_T5_stat_rf_error: '0'
C2_T6_stat_rf_error: '0'
C1_T1_stat_battery_error: '0'
C1_T2_stat_battery_error: '0'
C1_T3_stat_battery_error: '0'
C1_T4_stat_battery_error: '0'
C1_T5_stat_battery_error: '0'
C1_T6_stat_battery_error: '0'
C1_T7_stat_battery_error: '0'
C2_T1_stat_battery_error: '0'
C2_T2_stat_battery_error: '0'
C2_T3_stat_battery_error: '0'
C2_T4_stat_battery_error: '0'
C2_T5_stat_battery_error: '0'
C2_T6_stat_battery_error: '0'
C1_T1_stat_rf_low_sig_warning: '0'
C1_T2_stat_rf_low_sig_warning: '0'
C1_T3_stat_rf_low_sig_warning: '0'
C1_T4_stat_rf_low_sig_warning: '0'
C1_T5_stat_rf_low_sig_warning: '0'
C1_T6_stat_rf_low_sig_warning: '0'
C1_T7_stat_rf_low_sig_warning: '0'
C2_T1_stat_rf_low_sig_warning: '0'
C2_T2_stat_rf_low_sig_warning: '0'
C2_T3_stat_rf_low_sig_warning: '0'
C2_T4_stat_rf_low_sig_warning: '0'
C2_T5_stat_rf_low_sig_warning: '0'
C2_T6_stat_rf_low_sig_warning: '0'
C1_T1_stat_valve_position_err: '0'
C1_T2_stat_valve_position_err: '0'
C1_T3_stat_valve_position_err: '0'
C1_T4_stat_valve_position_err: '0'
C1_T5_stat_valve_position_err: '0'
C1_T6_stat_valve_position_err: '0'
C1_T7_stat_valve_position_err: '0'
C2_T1_stat_valve_position_err: '0'
C2_T2_stat_valve_position_err: '0'
C2_T3_stat_valve_position_err: '0'
C2_T4_stat_valve_position_err: '0'
C2_T5_stat_valve_position_err: '0'
C2_T6_stat_valve_position_err: '0'
C1_T1_stat_eco_program: '0'
C1_T2_stat_eco_program: '0'
C1_T3_stat_eco_program: '0'
C1_T4_stat_eco_program: '0'
C1_T5_stat_eco_program: '0'
C1_T6_stat_eco_program: '0'
C1_T7_stat_eco_program: '0'
C2_T1_stat_eco_program: '0'
C2_T2_stat_eco_program: '0'
C2_T3_stat_eco_program: '0'
C2_T4_stat_eco_program: '0'
C2_T5_stat_eco_program: '0'
C2_T6_stat_eco_program: '0'
C1_T1_stat_demand_led: '0'
C1_T2_stat_demand_led: '0'
C1_T3_stat_demand_led: '0'
C1_T4_stat_demand_led: '0'
C1_T5_stat_demand_led: '0'
C1_T6_stat_demand_led: '0'
C1_T7_stat_demand_led: '1'
C2_T1_stat_demand_led: '0'
C2_T2_stat_demand_led: '0'
C2_T3_stat_demand_led: '0'
C2_T4_stat_demand_led: '0'
C2_T5_stat_demand_led: '0'
C2_T6_stat_demand_led: '0'
C1_T1_thermostat_type: '0'
C1_T2_thermostat_type: '0'
C1_T3_thermostat_type: '0'
C1_T4_thermostat_type: '0'
C1_T5_thermostat_type: '0'
C1_T6_thermostat_type: '0'
C1_T7_thermostat_type: '0'
C2_T1_thermostat_type: '0'
C2_T2_thermostat_type: '0'
C2_T3_thermostat_type: '0'
C2_T4_thermostat_type: '0'
C2_T5_thermostat_type: '0'
C2_T6_thermostat_type: '0'
C1_T1_eco_setting: '1'
C1_T2_eco_setting: '1'
C1_T3_eco_setting: '1'
C1_T4_eco_setting: '1'
C1_T5_eco_setting: '1'
C1_T6_eco_setting: '1'
C1_T7_eco_setting: '1'
C2_T1_eco_setting: '1'
C2_T2_eco_setting: '1'
C2_T3_eco_setting: '1'
C2_T4_eco_setting: '1'
C2_T5_eco_setting: '1'
C2_T6_eco_setting: '0'
C1_T1_system_device_public: '0'
C1_T2_system_device_public: '0'
C1_T3_system_device_public: '0'
C1_T4_system_device_public: '0'
C1_T5_system_device_public: '0'
C1_T6_system_device_public: '0'
C1_T7_system_device_public: '0'
C2_T1_system_device_public: '0'
C2_T2_system_device_public: '0'
C2_T3_system_device_public: '0'
C2_T4_system_device_public: '0'
C2_T5_system_device_public: '0'
C2_T6_system_device_public: '0'
C1_T1_input_state: '0'
C1_T2_input_state: '0'
C1_T3_input_state: '0'
C1_T4_input_state: '0'
C1_T5_input_state: '0'
C1_T6_input_state: '0'
C1_T7_input_state: '0'
C2_T1_input_state: '0'
C2_T2_input_state: '0'
C2_T3_input_state: '0'
C2_T4_input_state: '0'
C2_T5_input_state: '0'
C2_T6_input_state: '0'
C1_T1_sensor_only: '0'
C1_T2_sensor_only: '0'
C1_T3_sensor_only: '0'
C1_T4_sensor_only: '0'
C1_T5_sensor_only: '0'
C1_T6_sensor_only: '0'
C1_T7_sensor_only: '0'
C2_T1_sensor_only: '0'
C2_T2_sensor_only: '0'
C2_T3_sensor_only: '0'
C2_T4_sensor_only: '0'
C2_T5_sensor_only: '0'
C2_T6_sensor_only: '0'
C1_T1_regulation_mode: '0'
C1_T2_regulation_mode: '0'
C1_T3_regulation_mode: '0'
C1_T4_regulation_mode: '0'
C1_T5_regulation_mode: '0'
C1_T6_regulation_mode: '0'
C1_T7_regulation_mode: '0'
C2_T1_regulation_mode: '0'
C2_T2_regulation_mode: '0'
C2_T3_regulation_mode: '0'
C2_T4_regulation_mode: '0'
C2_T5_regulation_mode: '0'
C2_T6_regulation_mode: '0'
C1_T1_cool_allowed: '1'
C1_T2_cool_allowed: '1'
C1_T3_cool_allowed: '1'
C1_T4_cool_allowed: '1'
C1_T5_cool_allowed: '1'
C1_T6_cool_allowed: '1'
C1_T7_cool_allowed: '1'
C2_T1_cool_allowed: '1'
C2_T2_cool_allowed: '1'
C2_T3_cool_allowed: '1'
C2_T4_cool_allowed: '1'
C2_T5_cool_allowed: '1'
C2_T6_cool_allowed: '1'
C1_T1_manual_cool_allowed: '0'
C1_T2_manual_cool_allowed: '1'
C1_T3_manual_cool_allowed: '0'
C1_T4_manual_cool_allowed: '1'
C1_T5_manual_cool_allowed: '0'
C1_T6_manual_cool_allowed: '0'
C1_T7_manual_cool_allowed: '0'
C2_T1_manual_cool_allowed: '0'
C2_T2_manual_cool_allowed: '0'
C2_T3_manual_cool_allowed: '0'
C2_T4_manual_cool_allowed: '1'
C2_T5_manual_cool_allowed: '0'
C2_T6_manual_cool_allowed: '0'
C1_T1_heat_cool_mode: '0'
C1_T2_heat_cool_mode: '0'
C1_T3_heat_cool_mode: '0'
C1_T4_heat_cool_mode: '0'
C1_T5_heat_cool_mode: '0'
C1_T6_heat_cool_mode: '0'
C1_T7_heat_cool_mode: '0'
C2_T1_heat_cool_mode: '0'
C2_T2_heat_cool_mode: '0'
C2_T3_heat_cool_mode: '0'
C2_T4_heat_cool_mode: '0'
C2_T5_heat_cool_mode: '0'
C2_T6_heat_cool_mode: '0'
C1_T1_heat_cool_slave: '0'
C1_T2_heat_cool_slave: '0'
C1_T3_heat_cool_slave: '0'
C1_T4_heat_cool_slave: '0'
C1_T5_heat_cool_slave: '0'
C1_T6_heat_cool_slave: '0'
C1_T7_heat_cool_slave: '0'
C2_T1_heat_cool_slave: '0'
C2_T2_heat_cool_slave: '0'
C2_T3_heat_cool_slave: '0'
C2_T4_heat_cool_slave: '0'
C2_T5_heat_cool_slave: '0'
C2_T6_heat_cool_slave: '0'
C1_T1_room_temperature: '746'
C1_T2_room_temperature: '756'
C1_T3_room_temperature: '741'
C1_T4_room_temperature: '739'
C1_T5_room_temperature: '742'
C1_T6_room_temperature: '761'
C1_T7_room_temperature: '771'
C2_T1_room_temperature: '750'
C2_T2_room_temperature: '752'
C2_T3_room_temperature: '763'
C2_T4_room_temperature: '773'
C2_T5_room_temperature: '775'
C2_T6_room_temperature: '744'
C1_T1_external_temperature: '32767'
C1_T2_external_temperature: '32767'
C1_T3_external_temperature: '32767'
C1_T4_external_temperature: '32767'
C1_T5_external_temperature: '32767'
C1_T6_external_temperature: '32767'
C1_T7_external_temperature: '32767'
C2_T1_external_temperature: '32767'
C2_T2_external_temperature: '32767'
C2_T3_external_temperature: '32767'
C2_T4_external_temperature: '32767'
C2_T5_external_temperature: '32767'
C2_T6_external_temperature: '32767'
C1_T1_rh: '0'
C1_T2_rh: '0'
C1_T3_rh: '0'
C1_T4_rh: '0'
C1_T5_rh: '0'
C1_T6_rh: '0'
C1_T7_rh: '0'
C2_T1_rh: '0'
C2_T2_rh: '0'
C2_T3_rh: '0'
C2_T4_rh: '0'
C2_T5_rh: '0'
C2_T6_rh: '0'
C1_T1_hw_type: '0'
C1_T2_hw_type: '0'
C1_T3_hw_type: '0'
C1_T4_hw_type: '0'
C1_T5_hw_type: '0'
C1_T6_hw_type: '0'
C1_T7_hw_type: '0'
C2_T1_hw_type: '0'
C2_T2_hw_type: '0'
C2_T3_hw_type: '0'
C2_T4_hw_type: '0'
C2_T5_hw_type: '0'
C2_T6_hw_type: '0'
C1_T1_sw_version: '11'
C1_T2_sw_version: '11'
C1_T3_sw_version: '11'
C1_T4_sw_version: '11'
C1_T5_sw_version: '11'
C1_T6_sw_version: '11'
C1_T7_sw_version: '11'
C2_T1_sw_version: '11'
C2_T2_sw_version: '11'
C2_T3_sw_version: '11'
C2_T4_sw_version: '11'
C2_T5_sw_version: '11'
C2_T6_sw_version: '11'
C1_T1_ufh_pwm_output: '50'
C1_T2_ufh_pwm_output: '50'
C1_T3_ufh_pwm_output: '50'
C1_T4_ufh_pwm_output: '50'
C1_T5_ufh_pwm_output: '50'
C1_T6_ufh_pwm_output: '50'
C1_T7_ufh_pwm_output: '50'
C2_T1_ufh_pwm_output: '50'
C2_T2_ufh_pwm_output: '50'
C2_T3_ufh_pwm_output: '50'
C2_T4_ufh_pwm_output: '50'
C2_T5_ufh_pwm_output: '50'
C2_T6_ufh_pwm_output: '50'
C1_T1_head1_supply_temp: '50'
C1_T2_head1_supply_temp: '50'
C1_T3_head1_supply_temp: '50'
C1_T4_head1_supply_temp: '50'
C1_T5_head1_supply_temp: '50'
C1_T6_head1_supply_temp: '50'
C1_T7_head1_supply_temp: '50'
C2_T1_head1_supply_temp: '50'
C2_T2_head1_supply_temp: '50'
C2_T3_head1_supply_temp: '50'
C2_T4_head1_supply_temp: '50'
C2_T5_head1_supply_temp: '50'
C2_T6_head1_supply_temp: '50'
C1_T1_head1_valve_pos_percent: '0'
C1_T2_head1_valve_pos_percent: '0'
C1_T3_head1_valve_pos_percent: '0'
C1_T4_head1_valve_pos_percent: '0'
C1_T5_head1_valve_pos_percent: '0'
C1_T6_head1_valve_pos_percent: '0'
C1_T7_head1_valve_pos_percent: '0'
C2_T1_head1_valve_pos_percent: '0'
C2_T2_head1_valve_pos_percent: '0'
C2_T3_head1_valve_pos_percent: '0'
C2_T4_head1_valve_pos_percent: '0'
C2_T5_head1_valve_pos_percent: '0'
C2_T6_head1_valve_pos_percent: '0'
C1_T1_head1_valve_pos: '0'
C1_T2_head1_valve_pos: '0'
C1_T3_head1_valve_pos: '0'
C1_T4_head1_valve_pos: '0'
C1_T5_head1_valve_pos: '0'
C1_T6_head1_valve_pos: '0'
C1_T7_head1_valve_pos: '0'
C2_T1_head1_valve_pos: '0'
C2_T2_head1_valve_pos: '0'
C2_T3_head1_valve_pos: '0'
C2_T4_head1_valve_pos: '0'
C2_T5_head1_valve_pos: '0'
C2_T6_head1_valve_pos: '0'
C1_T1_head1_sw_version: '0'
C1_T2_head1_sw_version: '0'
C1_T3_head1_sw_version: '0'
C1_T4_head1_sw_version: '0'
C1_T5_head1_sw_version: '0'
C1_T6_head1_sw_version: '0'
C1_T7_head1_sw_version: '0'
C2_T1_head1_sw_version: '0'
C2_T2_head1_sw_version: '0'
C2_T3_head1_sw_version: '0'
C2_T4_head1_sw_version: '0'
C2_T5_head1_sw_version: '0'
C2_T6_head1_sw_version: '0'
C1_T1_ufh1_actuator_cycle: '0'
C1_T2_ufh1_actuator_cycle: '0'
C1_T3_ufh1_actuator_cycle: '0'
C1_T4_ufh1_actuator_cycle: '0'
C1_T5_ufh1_actuator_cycle: '0'
C1_T6_ufh1_actuator_cycle: '0'
C1_T7_ufh1_actuator_cycle: '0'
C2_T1_ufh1_actuator_cycle: '0'
C2_T2_ufh1_actuator_cycle: '0'
C2_T3_ufh1_actuator_cycle: '0'
C2_T4_ufh1_actuator_cycle: '0'
C2_T5_ufh1_actuator_cycle: '0'
C2_T6_ufh1_actuator_cycle: '0'
C1_T1_head2_valve_pos_percent: '0'
C1_T2_head2_valve_pos_percent: '0'
C1_T3_head2_valve_pos_percent: '0'
C1_T4_head2_valve_pos_percent: '0'
C1_T5_head2_valve_pos_percent: '0'
C1_T6_head2_valve_pos_percent: '0'
C1_T7_head2_valve_pos_percent: '0'
C2_T1_head2_valve_pos_percent: '0'
C2_T2_head2_valve_pos_percent: '0'
C2_T3_head2_valve_pos_percent: '0'
C2_T4_head2_valve_pos_percent: '0'
C2_T5_head2_valve_pos_percent: '0'
C2_T6_head2_valve_pos_percent: '0'
C1_T1_head2_valve_pos: '0'
C1_T2_head2_valve_pos: '0'
C1_T3_head2_valve_pos: '0'
C1_T4_head2_valve_pos: '0'
C1_T5_head2_valve_pos: '0'
C1_T6_head2_valve_pos: '0'
C1_T7_head2_valve_pos: '0'
C2_T1_head2_valve_pos: '0'
C2_T2_head2_valve_pos: '0'
C2_T3_head2_valve_pos: '0'
C2_T4_head2_valve_pos: '0'
C2_T5_head2_valve_pos: '0'
C2_T6_head2_valve_pos: '0'
C1_T1_head2_sw_version: '0'
C1_T2_head2_sw_version: '0'
C1_T3_head2_sw_version: '0'
C1_T4_head2_sw_version: '0'
C1_T5_head2_sw_version: '0'
C1_T6_head2_sw_version: '0'
C1_T7_head2_sw_version: '0'
C2_T1_head2_sw_version: '0'
C2_T2_head2_sw_version: '0'
C2_T3_head2_sw_version: '0'
C2_T4_head2_sw_version: '0'
C2_T5_head2_sw_version: '0'
C2_T6_head2_sw_version: '0'
C1_T1_ufh2_actuator_cycle: '0'
C1_T2_ufh2_actuator_cycle: '0'
C1_T3_ufh2_actuator_cycle: '0'
C1_T4_ufh2_actuator_cycle: '0'
C1_T5_ufh2_actuator_cycle: '0'
C1_T6_ufh2_actuator_cycle: '0'
C1_T7_ufh2_actuator_cycle: '0'
C2_T1_ufh2_actuator_cycle: '0'
C2_T2_ufh2_actuator_cycle: '0'
C2_T3_ufh2_actuator_cycle: '0'
C2_T4_ufh2_actuator_cycle: '0'
C2_T5_ufh2_actuator_cycle: '0'
C2_T6_ufh2_actuator_cycle: '0'
C1_T1_head2_supply_temp: '0'
C1_T2_head2_supply_temp: '0'
C1_T3_head2_supply_temp: '0'
C1_T4_head2_supply_temp: '0'
C1_T5_head2_supply_temp: '0'
C1_T6_head2_supply_temp: '0'
C1_T7_head2_supply_temp: '0'
C2_T1_head2_supply_temp: '0'
C2_T2_head2_supply_temp: '0'
C2_T3_head2_supply_temp: '0'
C2_T4_head2_supply_temp: '0'
C2_T5_head2_supply_temp: '0'
C2_T6_head2_supply_temp: '0'
C1_T1_channel_position: '1'
C1_T2_channel_position: '2'
C1_T3_channel_position: '4'
C1_T4_channel_position: '8'
C1_T5_channel_position: '16'
C1_T6_channel_position: '32'
C1_T7_channel_position: '64'
C2_T1_channel_position: '1'
C2_T2_channel_position: '2'
C2_T3_channel_position: '4'
C2_T4_channel_position: '8'
C2_T5_channel_position: '16'
C2_T6_channel_position: '32'
C1_T1_head_number: '0'
C1_T2_head_number: '0'
C1_T3_head_number: '0'
C1_T4_head_number: '0'
C1_T5_head_number: '0'
C1_T6_head_number: '0'
C1_T7_head_number: '0'
C2_T1_head_number: '0'
C2_T2_head_number: '0'
C2_T3_head_number: '0'
C2_T4_head_number: '0'
C2_T5_head_number: '0'
C2_T6_head_number: '0'
C1_id_output_module: '0'
C2_id_output_module: '0'
C1_id_sys_dev_outdoor: '0'
C2_id_sys_dev_outdoor: '0'
C1_id_sys_dev_hc: '0'
C2_id_sys_dev_hc: '0'
C1_id_sys_dev_eco: '0'
C2_id_sys_dev_eco: '0'
C1_thermostat1_id: '<redigerat>'
C2_thermostat1_id: '<redigerat>'
C1_thermostat2_id: '<redigerat>'
C2_thermostat2_id: '<redigerat>'
C1_thermostat3_id: '<redigerat>'
C2_thermostat3_id: '<redigerat>'
C1_thermostat4_id: '<redigerat>'
C2_thermostat4_id: '<redigerat>'
C1_thermostat5_id: '<redigerat>'
C2_thermostat5_id: '<redigerat>'
C1_thermostat6_id: '<redigerat>'
C2_thermostat6_id: '<redigerat>'
C1_thermostat7_id: '<redigerat>'
C2_thermostat7_id: '0'
C1_thermostat8_id: '0'
C2_thermostat8_id: '0'
C1_thermostat9_id: '0'
C2_thermostat9_id: '0'
C1_thermostat10_id: '0'
C2_thermostat10_id: '0'
C1_thermostat11_id: '0'
C2_thermostat11_id: '0'
C1_thermostat12_id: '0'
C2_thermostat12_id: '0'
C1_TTH_1_id: '0'
C2_TTH_1_id: '0'
C1_TTH_2_id: '0'
C2_TTH_2_id: '0'
C1_TTH_3_id: '0'
C2_TTH_3_id: '0'
C1_TTH_4_id: '0'
C2_TTH_4_id: '0'
C1_TTH_5_id: '0'
C2_TTH_5_id: '0'
C1_TTH_6_id: '0'
C2_TTH_6_id: '0'
C1_TTH_7_id: '0'
C2_TTH_7_id: '0'
C1_TTH_8_id: '0'
C2_TTH_8_id: '0'
C1_TTH_9_id: '0'
C2_TTH_9_id: '0'
C1_TTH_10_id: '0'
C2_TTH_10_id: '0'
C1_TTH_11_id: '0'
C2_TTH_11_id: '0'
C1_TTH_12_id: '0'
C2_TTH_12_id: '0'
C1_TTH_13_id: '0'
C2_TTH_13_id: '0'
C1_TTH_14_id: '0'
C2_TTH_14_id: '0'
C1_TTH_15_id: '0'
C2_TTH_15_id: '0'
C1_TTH_16_id: '0'
C2_TTH_16_id: '0'
C1_TTH_17_id: '0'
C2_TTH_17_id: '0'
C1_TTH_18_id: '0'
C2_TTH_18_id: '0'
C1_TTH_19_id: '0'
C2_TTH_19_id: '0'
C1_TTH_20_id: '0'
C2_TTH_20_id: '0'
C1_TTH_21_id: '0'
C2_TTH_21_id: '0'
C1_TTH_22_id: '0'
C2_TTH_22_id: '0'
C1_TTH_23_id: '0'
C2_TTH_23_id: '0'
C1_TTH_24_id: '0'
C2_TTH_24_id: '0'
controller1_id: '<redigerat>'
controller2_id: '<redigerat>'
controller3_id: '0'
controller4_id: '0'
C1_T1_Monday: c0ffffffffff
C1_T2_Monday: '000000000000'
C1_T3_Monday: '000000000000'
C1_T4_Monday: '000000000000'
C1_T5_Monday: '000000000000'
C1_T6_Monday: '000000000000'
C1_T7_Monday: '000000000000'
C2_T1_Monday: '000000000000'
C2_T2_Monday: '000000000000'
C2_T3_Monday: '000000000000'
C2_T4_Monday: '000000000000'
C2_T5_Monday: '000000000000'
C2_T6_Monday: '000000000000'
C1_T1_Tuesday: ffffffffffff
C1_T2_Tuesday: '000000000000'
C1_T3_Tuesday: '000000000000'
C1_T4_Tuesday: '000000000000'
C1_T5_Tuesday: '000000000000'
C1_T6_Tuesday: '000000000000'
C1_T7_Tuesday: '000000000000'
C2_T1_Tuesday: '000000000000'
C2_T2_Tuesday: '000000000000'
C2_T3_Tuesday: '000000000000'
C2_T4_Tuesday: '000000000000'
C2_T5_Tuesday: '000000000000'
C2_T6_Tuesday: '000000000000'
C1_T1_Wednesday: ffffffffffff
C1_T2_Wednesday: '000000000000'
C1_T3_Wednesday: '000000000000'
C1_T4_Wednesday: '000000000000'
C1_T5_Wednesday: '000000000000'
C1_T6_Wednesday: '000000000000'
C1_T7_Wednesday: '000000000000'
C2_T1_Wednesday: '000000000000'
C2_T2_Wednesday: '000000000000'
C2_T3_Wednesday: '000000000000'
C2_T4_Wednesday: '000000000000'
C2_T5_Wednesday: '000000000000'
C2_T6_Wednesday: '000000000000'
C1_T1_Thursday: ffffffffffff
C1_T2_Thursday: '000000000000'
C1_T3_Thursday: '000000000000'
C1_T4_Thursday: '000000000000'
C1_T5_Thursday: '000000000000'
C1_T6_Thursday: '000000000000'
C1_T7_Thursday: '000000000000'
C2_T1_Thursday: '000000000000'
C2_T2_Thursday: '000000000000'
C2_T3_Thursday: '000000000000'
C2_T4_Thursday: '000000000000'
C2_T5_Thursday: '000000000000'
C2_T6_Thursday: '000000000000'
C1_T1_Friday: ffffffffffff
C1_T2_Friday: '000000000000'
C1_T3_Friday: '000000000000'
C1_T4_Friday: '000000000000'
C1_T5_Friday: '000000000000'
C1_T6_Friday: '000000000000'
C1_T7_Friday: '000000000000'
C2_T1_Friday: '000000000000'
C2_T2_Friday: '000000000000'
C2_T3_Friday: '000000000000'
C2_T4_Friday: '000000000000'
C2_T5_Friday: '000000000000'
C2_T6_Friday: '000000000000'
C1_T1_Saturday: ffffffffffff
C1_T2_Saturday: '000000000000'
C1_T3_Saturday: '000000000000'
C1_T4_Saturday: '000000000000'
C1_T5_Saturday: '000000000000'
C1_T6_Saturday: '000000000000'
C1_T7_Saturday: '000000000000'
C2_T1_Saturday: '000000000000'
C2_T2_Saturday: '000000000000'
C2_T3_Saturday: '000000000000'
C2_T4_Saturday: '000000000000'
C2_T5_Saturday: '000000000000'
C2_T6_Saturday: '000000000000'
C1_T1_Sunday: ffffffffffff
C1_T2_Sunday: '000000000000'
C1_T3_Sunday: '000000000000'
C1_T4_Sunday: '000000000000'
C1_T5_Sunday: '000000000000'
C1_T6_Sunday: '000000000000'
C1_T7_Sunday: '000000000000'
C2_T1_Sunday: '000000000000'
C2_T2_Sunday: '000000000000'
C2_T3_Sunday: '000000000000'
C2_T4_Sunday: '000000000000'
C2_T5_Sunday: '000000000000'
C2_T6_Sunday: '000000000000'
cust_C1_T1_Custom_Eco_Profile: '1'
```

</details>

---

## Hårdvara som stöds

Integrationen stöder **Uponor Smatrix Wave Pulse (X-265)** och **Uponor Smatrix Base Pulse (X-245)**. Reglercentralen (X-265/X-245) är gatewayen som integrationen pratar JNAP med. Termostaterna blir `climate`-entiteter i HA; dial-modellerna T-144/T-145 (se [const.py](custom_components/uponorx265/const.py) `DIAL_THERMOSTAT_MODELS`) kräver "HA controlled"-läge för fjärrstyrning av börvärde.

### Uponor Smatrix Wave Pulse (X-265)

| Produkt | Typ |
|---|---|
| Uponor Smatrix A-1XX | Transformatormodul |
| Uponor Smatrix Wave Pulse X-265 | Reglercentral (gateway) |
| Uponor Smatrix Wave Pulse M-262 | Kopplingsmodul |
| Uponor Smatrix Wave Pulse A-265 | Antenn |
| Uponor Smatrix Pulse Com R-208 | Kommunikationsmodul |
| Uponor Smatrix Wave T-169 | Digital termostat, med sensor för relativ luftfuktighet och drift |
| Uponor Smatrix Wave T-168 | Programmerbar digital termostat, med givare för relativ luftfuktighet |
| Uponor Smatrix Wave T-166 | Digital termostat |
| Uponor Smatrix Wave T-165 | Standardtermostat med tryckt skala på ratt |
| Uponor Smatrix Wave T-163 | Termostat för offentliga miljöer |
| Uponor Smatrix Wave T-162 | Termostathuvud |
| Uponor Smatrix Wave T-161 | Rumsgivartermostat, med givare för relativ luftfuktighet och drift |
| Uponor Smatrix Wave M-161 | Relämodul |

### Uponor Smatrix Base Pulse (X-245)

| Produkt | Typ |
|---|---|
| Uponor Smatrix A-1XX | Transformatormodul |
| Uponor Smatrix Base Pulse X-245 | Reglercentral (gateway) |
| Uponor Smatrix Base Pulse M-242 | Kopplingsmodul |
| Uponor Smatrix Base Pulse M-243 | Stjärnmodul |
| Uponor Smatrix Pulse Com R-208 | Kommunikationsmodul |
| Uponor Smatrix Base T-149 | Digital termostat, med sensor för relativ luftfuktighet och drift |
| Uponor Smatrix Base T-148 | Programmerbar digital termostat, med givare för relativ luftfuktighet |
| Uponor Smatrix Base T-146 | Digital termostat |
| Uponor Smatrix Base T-145 | Standardtermostat med tryckt skala på ratt |
| Uponor Smatrix Base T-144 | Infälld termostat |
| Uponor Smatrix Base T-143 | Termostat för offentliga miljöer |
| Uponor Smatrix Base T-141 | Rumsgivartermostat, med givare för relativ luftfuktighet och drift |

### Identifiering av termostatmodell

JNAP-gatewayen exponerar inte modellnamnet på termostaterna direkt — bara ett serienummer (`C?_thermostatN_id`) och en rå hårdvarutypkod (`C?_T?_thermostat_type`). Integrationen gissar modellen från dessa i [`_detect_thermostat_model()`](custom_components/uponorx265/__init__.py) (`__init__.py`):

- Hårdvarutypkoden (`hwid`) är det första urvalskriteriet:
  - `hwid == 2` → **T-146** (fältbekräftat på `sn`-prefix `285`).
  - `hwid == 0` → T-144/T-145-familjen, som delar samma `hwid` och behöver särskiljas via serienumret.
- För `hwid == 0` bryts serienumrets första 4 siffror (`sn`) upp i prefix (`prodk`, första 3 siffrorna) och sista siffran (`mod`):
  - **Känd regel:** prefix `269` → sista siffran `1` = T-144, sista siffran `2` = T-145.
  - **Catch-all:** alla övriga `hwid == 0`-enheter (okänt prefix, eller `269` med annan slutsiffra) defaultar till **T-145** — samma beteende som Uponors egen app tycks ha när den inte kan skilja dem åt. Prefix `268` (fältbekräftat, `sn "2688"`) hanteras redan av catch-all:en men har en egen gren kvar som markering, ifall ett mönster framträder när fler termostater rapporterar in.
- Går identifieringen inte att göra alls (t.ex. saknad `thermostat_type`-variabel) faller den tillbaka på senast cachad modell (`get_thermostat_model()`), och i sista hand `None` — HA visar då ingen modell för enheten, men funktionen påverkas inte (endast `DIAL_THERMOSTAT_MODELS`-gating, se `requires_local_override()`).

Detta är reverse-engineering utan tillgång till Uponors officiella serienummerschema — det finns alltså ingen garanti att `hwid`/prefix-mönstret håller för hårdvara vi inte sett än. Ny hårdvara loggas via `dump_hardware_info`-tjänsten (`sn_start`, `hardware_type_raw`, `detected_model`) och kan skickas in för att förfina reglerna ovan.

### Komponentbeskrivningar (ur Uponors installationsmanual)

<details>
<summary><strong>Uponor Smatrix Base Pulse X-245</strong> (reglercentral)</summary>

- Integrerade Dynamic Energy Management (DEM)-funktioner, t.ex. autobalansering (aktiverad i utgångsläget). Övriga DEM-funktioner (komfortinställning, rum-bypass, övervakning av framledningstemperatur) kräver Pulse-appen (kommunikationsmodul) och i vissa fall Uponors molntjänster.
- Elektronisk styrning av styrdon, max åtta styrdon (24 V AC).
- Två-vägskommunikation med upp till sex rumstermostater.
- Omkoppling värme/kyla (avancerad) och/eller Komfort/ECO via sluten kontakt, termostat för offentliga miljöer eller Pulse-appen.
- Separata reläer för pump- och pannstyrning; övriga kontrollfunktioner kräver kommunikationsmodul + app.
- Ventil- och pumpmotion. Relativ fuktighetskontroll (kräver Pulse-appen).
- Styrning av kombinerad golvvärme/-kyla och takkyla (kräver kommunikationsmodul + app).
- ECO-läge sänker inomhustemp (värme) / höjer (kyla); aktiveras globalt via sluten kontakt, termostat för offentliga miljöer eller appen, eller per rum via programmerbar termostat/ECO-profiler.
- Tillval: kommunikationsmodul för appanslutning (fjärranslutning kräver Uponors molntjänster); kopplingsmodul (+6 termostatkanaler, +6 styrdonsutgångar); stjärnmodul (+8 anslutningsbussar); upp till fyra reglercentraler i ett system (kräver kommunikationsmodul + app); modulär placering med löstagbar transformator; montering i skåp/vägg (DIN-skena eller skruvar); valfri placering/orientering (kommunikationsmodulen måste dock monteras vertikalt).

</details>

<details>
<summary><strong>Uponor Smatrix Pulse Com R-208</strong> (kommunikationsmodul)</summary>

- Ger Uponor Smatrix Pulse-appanslutning via Wi-Fi eller Ethernet — det är denna modul som exponerar JNAP-gatewayen som integrationen pratar med.
- Extra funktioner via appen: inställningar för värme/kyla, ytterligare reläfunktioner (kylaggregat, avfuktare m.m.).
- Kan integrera upp till fyra reglercentraler i ett system.
- Montering i skåp eller på vägg (DIN-skena eller medföljande skruvar).

</details>

<details>
<summary><strong>Uponor Smatrix Base M-242</strong> (kopplingsmodul)</summary>

- Endast en kopplingsmodul per reglercentral.
- Plugin-installation i befintlig reglercentral, ingen extra kabeldragning.
- Registrerar upp till sex extra termostater och ansluter upp till sex extra styrdon (24 V).
- Elektronisk styrning, ventilmotion.

</details>

<details>
<summary><strong>Uponor Smatrix Base M-243</strong> (stjärnmodul)</summary>

- Endast en stjärnmodul per busstyp (termostat- och/eller systembuss) per reglercentral; en stjärnmodul hanterar bara en busstyp åt gången.
- Möjliggör stjärnnätsdragning i stället för bussnät — mer flexibel kabeldragning.
- Kräver en Base Pulse-reglercentral. Adderar 8 extra bussanslutningar. Endast insignaler från termostater tillåts.
- Ansluts direkt till reglercentralen eller kopplingsmodulen med kommunikationskabel.

</details>

<details>
<summary><strong>Uponor Smatrix Base T-141</strong> (rumsgivartermostat)</summary>

- Så liten som möjligt men reglerar ändå rumstemperaturen.
- Drifttemperatursensor för ökad komfort.
- Börvärde justerbart via appen (kräver kommunikationsmodul), 5–35 °C.
- Gränsvärde för relativ luftfuktighet visas i appen (kräver kommunikationsmodul).

</details>

<details>
<summary><strong>Uponor Smatrix Base T-143</strong> (termostat för offentliga miljöer)</summary>

- Ratten dold — måste lossas från väggen för att ställa in temperatur; utlöser manipulationslarm vid borttagning (om aktiverat, syns även i appen med kommunikationsmodul).
- Kan registreras som systemenhet — då avaktiveras intern rumssensor och extra funktioner blir tillgängliga.
- Börvärde 5–35 °C via potentiometer på baksidan.
- Slutande kontaktingång för påtvingat ECO-läge (som systemenhet).
- Valfri extra utomhustemperaturgivare; golvtemperaturgränser endast konfigurerbara via appen.
- DIP-switch för funktions-/givarläge samt aktivering av Komfort/ECO-schema.

</details>

<details>
<summary><strong>Uponor Smatrix Base T-144</strong> (infälld termostat)</summary>

- Speciellt utformad för väggmontage (infälld installation), stor ratt med tryckt skala, 21 °C markerat.
- Max/min-temperatur endast inställbart via appen. Börvärde 5–35 °C.
- LED-indikering (~60 s) vid värme-/kylbehov.
- DIP-switch under ratten för Komfort/ECO-schemaläggning.
- Olika installationsramar för switchskeneram.

</details>

<details>
<summary><strong>Uponor Smatrix Base T-145</strong> (standardtermostat)</summary>

- Stor ratt med tryckt skala, 21 °C markerat, LED-ring indikerar börvärdesändring vid vridning.
- Max/min-temperatur endast inställbart via appen. Börvärde 5–35 °C.
- LED nedre högra hörnet indikerar (~60 s) värme-/kylbehov.
- DIP-switch på baksidan för Komfort/ECO-schemaläggning.

</details>

<details>
<summary><strong>Uponor Smatrix Base T-146</strong> (digital termostat med display)</summary>

- Upplyst display (släcks efter 10 s inaktivitet), visar °C/°F, kalibrerbar rumstemperatur, visar värme-/kylbehov och mjukvaruversion vid uppstart.
- Börvärde 5–35 °C. Stöd för externa temperaturgivare (tillval).
- Schemaläggning Komfort/ECO kräver Pulse-appen. Justerbar ECO-temperatursänkning.

</details>

<details>
<summary><strong>Uponor Smatrix Base T-148</strong> (programmerbar digital termostat)</summary>

- Display visar rumstemperatur, börvärde eller relativ luftfuktighet samt aktuell tid.
- Rekommenderas endast i system **utan** kommunikationsmodul — den egna schemaläggningsfunktionen stängs av om en kommunikationsmodul finns i systemet.
- Installationsguide för tid/datum, 12/24h-klocka, internminne mot strömavbrott.
- Börvärde 5–35 °C, stöd för externa temperaturgivare.
- Programmerbar Komfort/ECO-växling med eget ECO-värde; T-148 kan inte åsidosättas av andra systeminställningar när programmerad.
- Gränsvärdeslarm för luftfuktighet på display (kräver kommunikationsmodul).

</details>

<details>
<summary><strong>Uponor Smatrix Base T-149</strong> (e-papperstermostat)</summary>

- Strömsnål e-pappersdisplay, uppdateras var 10:e minut. Visar °C/°F, rumstemperatur, börvärde eller relativ luftfuktighet.
- Justering via +/- -knappar på sidan. Drifttemperatursensor, kalibrerbar rumstemperatur.
- Visar Uponor-logotyp och mjukvaruversion vid uppstart. Börvärde 5–35 °C, stöd för externa temperaturgivare.
- Schemaläggning Komfort/ECO kräver Pulse-appen. Justerbar ECO-temperatursänkning.
- Gränsvärdeslarm för luftfuktighet på display (kräver kommunikationsmodul). Kan invertera displayfärger.

</details>
