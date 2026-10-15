"""Build powerbi/CareerOS.pbip: the Power BI report of docs/powerbi_dashboard_spec.md, as a Power BI Project.

    .venv\\Scripts\\python.exe tools\\build_powerbi_project.py [--folder EXPORT_FOLDER]

A Power BI Project is the text form of a .pbix. Power BI Desktop opens CareerOS.pbip directly, and can save it as a
.pbix. Two parts:
- CareerOS.SemanticModel (TMSL, model.bim): the 13 tables of the analytics export, their types, relationships, sort
  orders and the date table, all taken from careeros/analytics/export.py, plus every measure of the spec;
- CareerOS.Report (PBIR): the five report pages.

The model loads the CSV files from one Power Query parameter, ExportFolder. By default that is this repository's
samples\\analytics_demo (synthetic data); point it at your vault's exports\\analytics to see your own search, in
Transform data > Manage parameters. A project built on private data stays private: never commit or publish it.

Everything generated is deterministic (IDs are UUID5 of the object's name), so tests can rebuild and compare it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from careeros.analytics.export import REFERENCES, TABLES  # noqa: E402

OUT = ROOT / "powerbi"
NAME = "CareerOS"
NAMESPACE = uuid.UUID("6f1c2b7e-1d1a-4c55-9a3e-0c6b8a9f2a41")
THEME = "Fluent2-CY26SU09"     # a base theme Power BI Desktop ships (2.158); copied from its install
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric"

TMSL_TYPES = {"key": "string", "text": "string", "int": "int64", "flag": "int64", "num": "double", "date": "dateTime"}
M_TYPES = {"key": "type text", "text": "type text", "int": "Int64.Type", "flag": "Int64.Type", "num": "type number", "date": "type date"}
INACTIVE = {("fact_job_evaluation", c) for c in ("posted_date_key", "last_seen_date_key", "decided_date_key", "closed_date_key",
                                                 "label_date_key")} | {("fact_verification", "started_date_key"),
                                                                       ("fact_application", "last_change_date_key")}
SORT_BY = {("dim_date", "month_name"): "month", ("dim_date", "day_name"): "day_of_week",
           ("dim_role_family", "family_name"): "sort_order", ("dim_location", "scope_label"): "sort_order"}
FORMATS = {"candidate_fit": "0.0", "eligibility_score": "0.0", "career_value": "0.0", "freshness": "0.0", "apply_priority": "0.0",
           "skill_coverage": "0%", "pay_min": "#,0.##", "pay_max": "#,0.##", "annual_pay_min": "$#,0", "annual_pay_max": "$#,0",
           "days_to_decision": "0.0", "days_listed": "0.0", "days_in_process": "0.0", "date": "yyyy-mm-dd"}

COUNT, RATE, ONE_DECIMAL, MONEY = "#,0", "0.0%", "0.0", "$#,0"
# (name, DAX, format): the measures of docs/powerbi_dashboard_spec.md section 3, in the same words
MEASURES = [
    ("Jobs Discovered", "COUNTROWS ( fact_job_evaluation )", COUNT),
    ("Distinct Openings", "CALCULATE ( [Jobs Discovered], fact_job_evaluation[is_duplicate] = 0, fact_job_evaluation[is_variant] = 0 )", COUNT),
    ("Jobs Evaluated", "SUM ( fact_job_evaluation[is_evaluated] )", COUNT),
    ("Jobs Qualified", "SUM ( fact_job_evaluation[is_qualified] )", COUNT),
    ("Jobs Recommended", "SUM ( fact_job_evaluation[is_recommended] )", COUNT),
    ("Jobs Rejected", "SUM ( fact_job_evaluation[is_rejected] )", COUNT),
    ("Jobs Manual Review", "SUM ( fact_job_evaluation[is_manual_review] )", COUNT),
    ("Manual Review Rate", "DIVIDE ( [Jobs Manual Review], [Jobs Evaluated] )", RATE),
    ("Duplicates Prevented", "SUM ( fact_job_evaluation[is_duplicate] ) + SUM ( fact_job_evaluation[linked_openings] )", COUNT),
    ("Cross-Source Sightings", "SUM ( fact_job_observation[is_cross_source] )", COUNT),
    ("Verified Employers", "CALCULATE ( COUNTROWS ( dim_employer ), dim_employer[is_verified_employer] = 1 )", COUNT),
    ("Average Fit", "CALCULATE ( AVERAGE ( fact_job_evaluation[candidate_fit] ), fact_job_evaluation[is_scored] = 1 )", ONE_DECIMAL),
    ("Board Verification Rate",
     "VAR Judged =\n    CALCULATETABLE (\n        dim_board,\n        dim_board[collection] = \"allowed\",\n"
     "        NOT dim_board[verification_status] IN { \"historical\", \"retired\" }\n    )\nRETURN\n"
     "    DIVIDE ( COUNTROWS ( FILTER ( Judged, dim_board[verification_status] = \"active\" ) ), COUNTROWS ( Judged ) )", RATE),
    ("New-Board Verification Rate",
     "VAR NewBoards = CALCULATETABLE ( dim_board, dim_board[is_legacy] = 0, dim_board[collection] = \"allowed\" )\nRETURN\n"
     "    DIVIDE (\n        COUNTROWS ( FILTER ( NewBoards, dim_board[verification_status] IN { \"quarantined\", \"active\" } ) ),\n"
     "        COUNTROWS ( NewBoards )\n    )", RATE),
    ("Scan Reliability", "DIVIDE ( SUM ( fact_board_scan[is_complete] ), COUNTROWS ( fact_board_scan ) )", RATE),
    ("NIGHT Routed", "CALCULATE ( [Jobs Discovered], fact_job_evaluation[night_decision] = \"eligible\" )", COUNT),
    ("Applications", "COUNTROWS ( fact_application )", COUNT),
    ("Open Applications", "SUM ( fact_application[is_open] )", COUNT),
    ("Interview Rate", "DIVIDE ( SUM ( fact_application[reached_interview] ), [Applications] )", RATE),
    # supporting measures
    ("Qualified Rate", "DIVIDE ( [Jobs Qualified], [Jobs Evaluated] )", RATE),
    ("Rejection Rate", "DIVIDE ( [Jobs Rejected], [Jobs Evaluated] )", RATE),
    ("Recommended Rate", "DIVIDE ( [Jobs Recommended], [Jobs Evaluated] )", RATE),
    ("Jobs Decided", "CALCULATE ( [Jobs Evaluated], USERELATIONSHIP ( fact_job_evaluation[decided_date_key], dim_date[date_key] ) )", COUNT),
    ("Jobs Closed", "CALCULATE ( SUM ( fact_job_evaluation[is_closed] ),\n"
                    "            USERELATIONSHIP ( fact_job_evaluation[closed_date_key], dim_date[date_key] ) )", COUNT),
    ("Jobs Labelled", "CALCULATE ( COUNTROWS ( fact_job_evaluation ), NOT ISBLANK ( fact_job_evaluation[label] ),\n"
                      "            USERELATIONSHIP ( fact_job_evaluation[label_date_key], dim_date[date_key] ) )", COUNT),
    ("Average Days to Decision", "AVERAGE ( fact_job_evaluation[days_to_decision] )", ONE_DECIMAL),
    ("Average Days Listed", "AVERAGE ( fact_job_evaluation[days_listed] )", ONE_DECIMAL),
    ("Pay Stated Rate", "DIVIDE ( CALCULATE ( [Jobs Discovered], dim_job[is_pay_stated] = 1 ), [Jobs Discovered] )", RATE),
    ("Average Top of Pay Range (annual)",
     "CALCULATE ( AVERAGEX ( fact_job_evaluation, RELATED ( dim_job[annual_pay_max] ) ), fact_job_evaluation[is_scored] = 1 )", MONEY),
    ("Labelled Apply", "CALCULATE ( [Jobs Discovered], fact_job_evaluation[label] = \"apply\" )", COUNT),
    ("Applied", "SUM ( fact_job_evaluation[has_application] )", COUNT),
    ("Recommended Then Applied", "CALCULATE ( [Applied], fact_job_evaluation[is_recommended] = 1 )", COUNT),
    ("Response Rate", "DIVIDE ( SUM ( fact_application[got_answer] ), [Applications] )", RATE),
    ("Screening Rate", "DIVIDE ( SUM ( fact_application[reached_screening] ), [Applications] )", RATE),
    ("Offer Rate", "DIVIDE ( SUM ( fact_application[reached_offer] ), [Applications] )", RATE),
    ("Average Board Trust", "AVERAGE ( dim_board[board_trust_score] )", ONE_DECIMAL),
    ("Scan Failures", "SUM ( fact_board_scan[is_failure] )", COUNT),
    ("Postings Read", "SUM ( fact_board_scan[postings] )", COUNT),
    ("Verification Runs", "COUNTROWS ( fact_verification )", COUNT),
    ("Verification Pass Rate", "DIVIDE ( SUM ( fact_verification[is_pass] ), [Verification Runs] )", RATE),
    ("Remote Nationwide Share",
     "DIVIDE ( CALCULATE ( [Jobs Discovered], dim_location[remote_scope] = \"US_nationwide\" ), [Jobs Discovered] )", RATE),
    # helpers for the report's visuals
    ("Boards", "CALCULATE ( COUNTROWS ( dim_board ), dim_board[board_key] <> \"(none)\" )", COUNT),
    ("Average Career Fit", "AVERAGE ( dim_employer[career_fit_score] )", ONE_DECIMAL),
    ("Shortlist Jobs", "CALCULATE ( COUNTROWS ( fact_job_evaluation ),\n"
                       "            fact_job_evaluation[is_recommended] = 1 || fact_job_evaluation[is_manual_review] = 1 )", COUNT),
    # by-reason charts: the empty reason of jobs that were not rejected (or scans that did not fail) is left out, totals kept
    ("Jobs Rejected by Reason",
     "IF ( HASONEVALUE ( fact_job_evaluation[rejection_group] ) && ISBLANK ( SELECTEDVALUE ( fact_job_evaluation[rejection_group] ) ),\n"
     "     BLANK (), [Jobs Rejected] )", COUNT),
    ("Scan Failures by Error",
     "IF ( HASONEVALUE ( fact_board_scan[error_code] ) && ISBLANK ( SELECTEDVALUE ( fact_board_scan[error_code] ) ),\n"
     "     BLANK (), [Scan Failures] )", COUNT),
]


def gid(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "/".join(parts)))


# --- semantic model ----------------------------------------------------------------------------------------------------
def load_table_function() -> list[str]:
    return ["(name as text) as table =>",
            "let",
            "    Source = Csv.Document(File.Contents(ExportFolder & \"\\\" & name & \".csv\"),",
            "                          [Delimiter = \",\", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),",
            "    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),",
            "    NoMark = Table.TransformColumnNames(Headers, each Text.Replace(_, Character.FromNumber(65279), \"\")),",
            "    Nulls = Table.ReplaceValue(NoMark, \"\", null, Replacer.ReplaceValue, Table.ColumnNames(NoMark))",
            "in",
            "    Nulls"]


def table(name: str, columns: list) -> dict:
    cols = []
    for column, kind in columns:
        col = {"name": column, "dataType": TMSL_TYPES[kind], "sourceColumn": column, "lineageTag": gid("column", name, column),
               "summarizeBy": "none", "annotations": [{"name": "SummarizationSetBy", "value": "User"}]}
        if column.endswith("_key") or kind == "flag":
            col["isHidden"] = True          # keys and 0/1 flags are reached through measures (spec, section 2)
        if column in FORMATS:
            col["formatString"] = FORMATS[column]
        if (name, column) in SORT_BY:
            col["sortByColumn"] = SORT_BY[(name, column)]
        if name == "dim_date" and column == "date":
            col["isKey"] = True
        if kind == "date":
            col["annotations"].append({"name": "UnderlyingDateTimeDataType", "value": "Date"})
        cols.append(col)
    types = ", ".join(f'{{"{c}", {M_TYPES[k]}}}' for c, k in columns)
    out = {"name": name, "lineageTag": gid("table", name), "columns": cols,
           "partitions": [{"name": name, "mode": "import", "source": {"type": "m", "expression": [
               "let", f'    Source = LoadTable("{name}"),', f"    Typed = Table.TransformColumnTypes(Source, {{{types}}})", "in", "    Typed"]}}],
           "annotations": [{"name": "PBI_ResultType", "value": "Table"}]}
    if name == "dim_date":
        out["dataCategory"] = "Time"
    return out


def model(folder: str) -> dict:
    tables = [table(name, columns) for name, columns in TABLES.items()]
    tables.append({"name": "_Measures", "lineageTag": gid("table", "_Measures"),
                   "columns": [{"name": "Placeholder", "dataType": "string", "sourceColumn": "Placeholder", "isHidden": True,
                                "lineageTag": gid("column", "_Measures", "Placeholder"), "summarizeBy": "none"}],
                   "partitions": [{"name": "_Measures", "mode": "import",
                                   "source": {"type": "m", "expression": ["let", '    Source = #table(type table [Placeholder = text], {})', "in",
                                                                          "    Source"]}}],
                   "measures": [{"name": n, "expression": dax.split("\n") if "\n" in dax else dax, "formatString": fmt,
                                 "lineageTag": gid("measure", n)} for n, dax, fmt in MEASURES],
                   "annotations": [{"name": "PBI_ResultType", "value": "Table"}]})
    relationships = []
    for (fact, column), dim in REFERENCES.items():
        if fact.startswith("dim_"):
            continue   # dimension-to-dimension references are attributes, not relationships
        to_column = TABLES[dim][0][0]
        rel = {"name": gid("relationship", fact, column), "fromTable": fact, "fromColumn": column, "toTable": dim, "toColumn": to_column}
        if (fact, column) in INACTIVE:
            rel["isActive"] = False
        relationships.append(rel)
    folder_m = folder.replace('"', '""')
    expressions = [
        {"name": "ExportFolder", "kind": "m", "lineageTag": gid("expression", "ExportFolder"),
         "expression": f'"{folder_m}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
         "annotations": [{"name": "PBI_ResultType", "value": "Text"}]},
        {"name": "LoadTable", "kind": "m", "lineageTag": gid("expression", "LoadTable"), "expression": load_table_function(),
         "annotations": [{"name": "PBI_ResultType", "value": "Function"}]},
    ]
    order = ["ExportFolder", "LoadTable"] + list(TABLES) + ["_Measures"]
    return {"compatibilityLevel": 1567, "model": {
        "culture": "en-US", "dataAccessOptions": {"legacyRedirects": True, "returnErrorValuesAsNull": True},
        "defaultPowerBIDataSourceVersion": "powerBI_V3", "sourceQueryCulture": "en-US", "tables": tables,
        "relationships": relationships, "expressions": expressions,
        "annotations": [{"name": "__PBI_TimeIntelligenceEnabled", "value": "0"},
                        {"name": "PBI_QueryOrder", "value": json.dumps(order)}]}}


# --- report --------------------------------------------------------------------------------------------------------------
def field(ref: str) -> dict:
    """'[Measure Name]' or 'table[column]', optionally followed by ' as Header' -> a query projection."""
    ref, _, label = ref.partition(" as ")
    if ref.startswith("["):
        name = ref[1:-1]
        out = {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}}, "Property": name}},
               "queryRef": f"_Measures.{name}", "nativeQueryRef": name}
    else:
        entity, column = ref[:-1].split("[")
        out = {"field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": column}},
               "queryRef": f"{entity}.{column}", "nativeQueryRef": column}
    if label:
        out["displayName"] = label
    return out


def literal(text: str) -> dict:
    return {"expr": {"Literal": {"Value": "'" + text.replace("'", "''") + "'"}}}


def visual(name: str, kind: str, box: tuple, roles: dict, title: str | None = None, sort: str | None = None, z: int = 0,
           objects: dict | None = None) -> dict:
    x, y, w, h = box
    query = {"queryState": {role: {"projections": [field(r) for r in refs]} for role, refs in roles.items()}}
    if sort:
        query["sortDefinition"] = {"sort": [{"field": field(sort)["field"], "direction": "Descending"}], "isDefaultSort": False}
    body = {"visualType": kind, "query": query, "drillFilterOtherVisuals": True}
    if objects:     # format settings: {object: {property: literal}}
        body["objects"] = {key: [{"properties": {p: {"expr": {"Literal": {"Value": v}}} for p, v in props.items()}}]
                           for key, props in objects.items()}
    if title:
        body["visualContainerObjects"] = {"title": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}},
                                                                    "text": literal(title)}}]}
    return {"$schema": f"{SCHEMA}/item/report/definition/visualContainer/2.4.0/schema.json", "name": name,
            "position": {"x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": z}, "visual": body}


def cards(prefix: str, measures: list[str], y: int, height: int = 90, x0: int = 10, width: int = 1260) -> list[dict]:
    w = (width - 10 * (len(measures) - 1)) / len(measures)
    return [visual(f"{prefix}_card{n}", "card", (round(x0 + n * (w + 10)), y, round(w), height), {"Values": [f"[{m}]"]}, z=n)
            for n, m in enumerate(measures)]


RANGE = {"data": {"mode": "'Between'"}, "header": {"show": "false"}}       # slicers: the visual's title names the field
DROPDOWN = {"data": {"mode": "'Dropdown'"}, "header": {"show": "false"}}
BY_DAY = {"categoryAxis": {"axisType": "'Categorical'"}}                   # one labelled point per day, not a time scale

PAGES = [
    ("executive", "Executive overview", lambda: [
        visual("exec_slicer_date", "slicer", (10, 8, 300, 76), {"Values": ["dim_date[date]"]}, "Date", objects=RANGE),
        visual("exec_slicer_family", "slicer", (320, 8, 300, 76), {"Values": ["dim_role_family[family_name]"]}, "Role family",
               objects=DROPDOWN),
        *cards("exec", ["Jobs Discovered", "Jobs Qualified", "Jobs Recommended", "Jobs Rejected", "Duplicates Prevented",
                        "Verified Employers", "Average Fit", "Manual Review Rate"], 92),
        visual("exec_funnel", "funnel", (10, 190, 620, 250),
               {"Y": ["[Jobs Discovered]", "[Distinct Openings]", "[Jobs Evaluated]", "[Jobs Qualified]", "[Jobs Recommended]",
                      "[Labelled Apply]", "[Applied]"]}, "From discovery to application"),
        visual("exec_trend", "lineChart", (640, 190, 630, 250), {"Category": ["dim_date[date]"], "Y": ["[Jobs Discovered]", "[Jobs Qualified]"]},
               "Jobs discovered and qualified by first-seen date", objects=BY_DAY),
        visual("exec_rejections", "clusteredBarChart", (10, 450, 620, 260),
               {"Category": ["fact_job_evaluation[rejection_group] as Reason"], "Y": ["[Jobs Rejected by Reason] as Jobs rejected"]},
               "Why jobs are rejected", sort="[Jobs Rejected by Reason]"),
        visual("exec_sources", "donutChart", (640, 450, 630, 260), {"Category": ["dim_source[source_name]"], "Y": ["[Jobs Qualified]"]},
               "Where qualified jobs come from"),
    ]),
    ("employers", "Employer intelligence", lambda: [
        visual("emp_table", "tableEx", (10, 10, 760, 380), {"Values": [
            "dim_employer[employer_name] as Employer", "[Jobs Discovered] as Found", "[Jobs Qualified] as Qualified",
            "[Jobs Recommended] as A/B", "[Average Fit] as Avg fit", "dim_employer[career_fit_score] as Career fit",
            "dim_employer[career_priority] as Priority", "dim_employer[boards_active] as Boards", "[Scan Reliability] as Scans OK"]},
            "Employers", sort="[Jobs Qualified]"),
        visual("emp_scatter", "scatterChart", (780, 10, 490, 380), {"Category": ["dim_employer[employer_name]"], "X": ["[Average Career Fit]"],
                                                                    "Y": ["[Jobs Qualified]"], "Size": ["[Jobs Discovered]"]},
               "Career fit against qualified jobs"),
        visual("emp_boards", "pivotTable", (10, 400, 760, 310), {"Rows": ["dim_board[ats_key] as Provider"], "Columns": ["dim_board[verification_status] as Status"],
                                                                 "Values": ["[Boards]"]}, "Board verification by provider"),
        *cards("emp", ["Verified Employers", "Board Verification Rate", "Average Board Trust"], 400, height=96, x0=780, width=490),
        *cards("emp2", ["New-Board Verification Rate", "Scan Reliability"], 506, height=96, x0=780, width=490),
        *cards("emp3", ["Boards", "Postings Read"], 612, height=96, x0=780, width=490),
    ]),
    ("pipeline", "Job pipeline", lambda: [
        # a time-scale axis: undecided and open jobs have no decided or closed date, which a categorical axis shows as (Blank)
        visual("pipe_flow", "clusteredColumnChart", (10, 10, 520, 290),
               {"Category": ["dim_date[date]"], "Y": ["[Jobs Discovered]", "[Jobs Decided]", "[Jobs Closed]"]}, "Jobs in and out by day"),
        visual("pipe_apps", "clusteredBarChart", (540, 10, 230, 290), {"Category": ["fact_application[current_state] as Status"], "Y": ["[Applications]"]},
               "Applications by status"),
        *cards("pipe", ["Average Days to Decision", "Average Days Listed"], 10, height=140, x0=780, width=490),
        *cards("pipe2", ["Jobs Manual Review", "Shortlist Jobs"], 160, height=140, x0=780, width=490),
        visual("pipe_decisions", "pivotTable", (10, 310, 420, 400),
               {"Rows": ["fact_job_evaluation[decision_stage] as Stage", "fact_job_evaluation[gate_decision] as Decision",
                         "fact_job_evaluation[rejection_reason] as Reason"],
                "Values": ["[Jobs Discovered] as Jobs"]}, "How decisions are made"),
        visual("pipe_shortlist", "tableEx", (440, 310, 830, 290), {"Values": [
            "dim_job[title] as Title", "dim_employer[employer_name] as Employer", "fact_job_evaluation[tier] as Tier",
            "fact_job_evaluation[apply_priority] as Priority", "fact_job_evaluation[candidate_fit] as Fit",
            "fact_job_evaluation[decisive_unknowns] as To check", "fact_job_evaluation[label] as Label", "[Shortlist Jobs] as Jobs"]},
            "Shortlist: recommended and manual review", sort="fact_job_evaluation[apply_priority]"),
        *cards("pipe3", ["Response Rate", "Interview Rate"], 610, height=100, x0=440, width=830),
    ]),
    ("roles", "Role intelligence", lambda: [
        visual("role_table", "tableEx", (10, 10, 760, 340), {"Values": [
            "dim_role_family[family_name] as Family", "[Jobs Discovered] as Found", "[Jobs Qualified] as Qualified",
            "[Qualified Rate] as Qualified %", "[Average Fit] as Avg fit", "[Average Top of Pay Range (annual)] as Avg top pay",
            "[Pay Stated Rate] as Pay stated"]}, "Role families", sort="[Jobs Qualified]"),
        visual("role_remote", "hundredPercentStackedBarChart", (780, 10, 490, 340),
               {"Category": ["dim_role_family[family_name]"], "Series": ["dim_location[scope_label]"], "Y": ["[Jobs Discovered]"]},
               "What 'remote' means, by family"),
        visual("role_rejections", "pivotTable", (10, 360, 580, 350),
               {"Rows": ["dim_role_family[family_name] as Family"], "Columns": ["fact_job_evaluation[rejection_group] as Reason"],
                "Values": ["[Jobs Rejected by Reason] as Rejected"]},
               "Rejection reasons by family"),
        visual("role_night", "clusteredColumnChart", (600, 360, 170, 350),
               {"Category": ["fact_job_evaluation[night_mode]"], "Y": ["[NIGHT Routed]"]}, "NIGHT routing"),
        # bars, not columns: the family names are too long to label a column axis
        visual("role_pay", "clusteredBarChart", (780, 360, 490, 350),
               {"Category": ["dim_role_family[family_name]"], "Y": ["[Average Top of Pay Range (annual)]"]}, "Top of pay range by family"),
    ]),
    ("quality", "System quality", lambda: [
        *cards("qual", ["Board Verification Rate", "New-Board Verification Rate", "Scan Reliability", "Verification Pass Rate",
                        "Average Board Trust", "Duplicates Prevented", "Cross-Source Sightings", "Manual Review Rate"], 10),
        visual("qual_scans", "lineChart", (10, 110, 620, 300), {"Category": ["dim_date[date]"], "Y": ["[Scan Reliability]"]},
               "Scan reliability by day", objects=BY_DAY),
        visual("qual_errors", "clusteredBarChart", (640, 110, 630, 300), {"Category": ["fact_board_scan[error_code] as Error"], "Y": ["[Scan Failures by Error] as Failures"]},
               "Scan failures by error"),
        visual("qual_verify", "pivotTable", (10, 420, 620, 290), {"Rows": ["fact_verification[kind] as Kind"], "Columns": ["fact_verification[outcome] as Outcome"],
                                                                   "Values": ["[Verification Runs]"]}, "Verification runs"),
        visual("qual_boards", "tableEx", (640, 420, 630, 290), {"Values": [
            "dim_board[board_key] as Board", "dim_board[verification_status] as Status", "dim_board[board_trust_score] as Trust",
            "dim_board[board_health_score] as Health", "dim_board[board_activity] as Activity", "dim_board[paused_reason] as Paused",
            "[Boards] as Count"]}, "Board health"),   # the Boards measure leaves out the (none) member
    ]),
]


def report() -> dict[str, dict]:
    """Relative path -> JSON content, for the PBIR definition folder."""
    files = {
        "version.json": {"$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json", "version": "2.0.0"},
        "report.json": {
            "$schema": f"{SCHEMA}/item/report/definition/report/3.0.0/schema.json",
            "themeCollection": {"baseTheme": {"name": THEME, "reportVersionAtImport": {"visual": "2.4.0", "report": "3.0.0", "page": "2.0.0"},
                                              "type": "SharedResources"}},
            "resourcePackages": [{"name": "SharedResources", "type": "SharedResources",
                                  "items": [{"name": THEME, "path": f"BaseThemes/{THEME}.json", "type": "BaseTheme"}]}],
            "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True, "useEnhancedTooltips": True}},
        "pages/pages.json": {"$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.0.0/schema.json",
                             "pageOrder": [p[0] for p in PAGES], "activePageName": PAGES[0][0]},
    }
    for name, display, visuals in PAGES:
        files[f"pages/{name}/page.json"] = {"$schema": f"{SCHEMA}/item/report/definition/page/2.0.0/schema.json", "name": name,
                                            "displayName": display, "displayOption": "FitToPage", "height": 720, "width": 1280}
        for v in visuals():
            files[f"pages/{name}/visuals/{v['name']}/visual.json"] = v
    return files


# --- the project ---------------------------------------------------------------------------------------------------------
def theme_source() -> Path:
    import subprocess
    location = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                               "(Get-AppxPackage -Name Microsoft.MicrosoftPowerBIDesktop).InstallLocation"],
                              capture_output=True, text=True).stdout.strip()
    return Path(location) / "bin" / "WebView2Resources" / "minerva" / "sharedresources" / "BaseThemes" / f"{THEME}.json"


def write(json_files: dict[str, dict], root: Path) -> None:
    for relative, content in json_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(content, indent=2) + "\n").encode("utf-8"))


def build(out: Path, folder: str, theme: Path | None) -> None:
    """Write the project into out (replacing what this script wrote there before)."""
    for part in (f"{NAME}.SemanticModel", f"{NAME}.Report"):
        target = out / part
        keep = {".pbi"}                          # Desktop's local cache and settings are left alone
        if target.exists():
            for child in target.iterdir():
                if child.name not in keep:
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
    write({f"{NAME}.pbip": {"$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json", "version": "1.0",
                            "artifacts": [{"report": {"path": f"{NAME}.Report"}}], "settings": {"enableAutoRecovery": True}},
           f"{NAME}.SemanticModel/definition.pbism": {"$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
                                                      "version": "4.0", "settings": {}},
           f"{NAME}.SemanticModel/model.bim": model(folder),
           f"{NAME}.Report/definition.pbir": {"$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json", "version": "4.0",
                                              "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}}}, out)
    write({f"{NAME}.Report/definition/{k}": v for k, v in report().items()}, out)
    if theme is not None:
        target = out / f"{NAME}.Report" / "StaticResources" / "SharedResources" / "BaseThemes" / f"{THEME}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(theme, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folder", default=str(ROOT / "samples" / "analytics_demo"),
                        help="the export folder the report loads (default: this repository's demo sample)")
    args = parser.parse_args()
    theme = theme_source()
    if not theme.exists():
        print(f"Power BI Desktop's {THEME} theme was not found; install Power BI Desktop, or copy the theme file in by hand.")
        theme = None
    build(OUT, args.folder, theme)
    print(f"{OUT / (NAME + '.pbip')} written: {len(TABLES) + 1} tables, {len(MEASURES)} measures, {len(PAGES)} pages "
          f"(ExportFolder = {args.folder}). Open it in Power BI Desktop, then Home > Refresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
