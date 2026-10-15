# CareerOS: requirements traceability matrix

- Version: 1.0
- Date: 2026-10-13
- Companion to `business_requirements.md`.

Each row reads: **business problem → requirement → system component → test → demo evidence**.
- **Tests** are `file::test_name` under `tests/`. All of them pass (419 tests on 2026-10-15).
- **Demo evidence** says what a reviewer can observe:
  - "Today" is a command or report section in the working system.
  - "Demo Dn" is a step of the synthetic demo, `careeros demo tour` (`docs/demo_mode.md`), listed at the end. D10 does
    not exist yet.

## BP-1 Volume: too many postings across too many sources

| Requirement | Component | Tests | Demo evidence |
|---|---|---|---|
| FR-001 job-board APIs | `discovery/boards.py`, `discovery/http.py`, `cli.py: cmd_discover` | `test_pipeline::test_full_flow`; `test_v26_discovery::test_budget_caps_every_request` | Today: `careeros run` output. Demo D1-D2 |
| FR-002 alert emails | `discovery/alerts.py`, `gmail.py`, `imap.py` | `test_alerts::test_one_link_per_job_with_its_title_and_the_text_beside_it`; `test_gmail::test_alerts_become_leads_once_and_messages_are_remembered_not_stored`; `test_imap::test_imap_alerts_flow_into_leads_once`; `test_v25_audit::test_application_and_recruiter_mail_is_never_read_for_jobs` | Today: Leads section. Demo D1 (`.eml` fixture) |
| FR-003 recommendation inbox | `discovery/github_inbox.py` | `test_github_inbox::test_issue_is_graded_answered_closed_and_never_reread`; `test_v25_audit::test_issue_lines_carry_structured_notes_and_a_note_can_never_change_a_decision` | Today: issue replies. Demo D1 (fake inbox) |
| FR-004 import / paste | `discovery/saved.py`, `manual_intake.py`, `review/server.py` | `test_saved::test_job_posting_data_is_read_from_a_saved_page`; `test_v26_discovery::test_pasted_postings_are_graded_without_any_fetch`; `test_v26_discovery::test_paste_and_dismiss_through_the_review_page_without_any_fetch` | Today: `careeros paste`. Demo D1 |
| FR-005 site policy | `discovery/sources.py`, `config/site_policy.yaml` | `test_v22_primary::test_a_source_that_is_not_allowed_is_never_fetched`; `test_config::test_only_the_four_ats_and_employer_sites_are_automatic`; `..::test_every_source_status_is_a_known_word`; `test_alerts::test_readable_boards_are_graded_and_other_sites_are_only_listed` | Today: Leads section. Demo D1 |
| FR-006 single link | `cli.py: cmd_add_url` | `test_github_inbox::test_careers_page_links_resolve_to_greenhouse_or_known_jobs_and_bad_guesses_add_nothing` | Today: `careeros add-url` |
| DQ-001 hidden characters and escaped HTML | `normalize/posting.py` | `test_qualify::test_greenhouse_escaped_html_and_invisible_chars` | Demo D2 |
| NFR-005 polite collection | `http.py`, `safe_fetch.py` | `test_v26_discovery::test_long_retry_after_is_never_slept_through_and_access_denial_waits_its_window`; `test_board_verification::test_robots_size_type_scope_and_page_limits` | — |
| FR-023 closure only after complete scans | `pipeline.py: ingest`, `source_health.py` | `test_v26_discovery::test_failed_and_suspicious_scans_never_close_or_prune`; `test_pipeline::test_board_errors_never_close_jobs` | Today: Sources section. Demo D3 |
| PERF-001, PERF-002 | `runs` table, budgets in `http.py`, `safe_fetch.py` | budgets: `test_v26_discovery::test_budget_caps_every_request`; `test_board_verification::test_robots_size_type_scope_and_page_limits` (run time is measured, not tested) | Today: `runs` table durations |

## BP-2 Duplication: the same opening many times

| Requirement | Component | Tests | Demo evidence |
|---|---|---|---|
| FR-020 meaningful vs cosmetic | `openings.py`, `pipeline.py` | `test_v26_discovery::test_meaningful_versus_cosmetic_and_timestamps` | Today: "Reconsidered after a meaningful change". Demo D3 |
| FR-021 requisition-aware dedupe, variants | `pipeline.py: _duplicate_of`, `seen_index.py` | `test_v25_audit::test_one_fingerprint_per_requisition_across_sites_and_a_new_requisition_stays_distinct`; `test_v26_discovery::test_identical_titles_with_different_requisitions_stay_separate_and_both_can_be_applied_to`; `test_v22_primary::test_same_job_through_another_site_is_recognised` | Today: grouped cards. Demo D3 |
| FR-022 provenance, reposts | `openings.py`, observations table | `test_v26_discovery::test_every_inbox_and_alert_sighting_keeps_its_provenance`; `test_v26_discovery::test_reposts_and_reopenings_keep_application_history` | Demo D3 |
| DQ-002 tracking parameters | `seen_index.py`, `normalize/posting.py: canonical_url` | `test_v25_audit::test_tracking_parameters_never_create_a_new_fingerprint`; `test_alerts::test_tracking_is_removed_and_job_links_recognised` | Demo D3 |
| DQ-003 weaker sources never overwrite | `openings.py: conflicts` | `test_v26_discovery::test_weaker_sources_never_overwrite_verified_facts_and_conflicts_stay_visible` | Today: conflicts on shortlist cards |

## BP-3 Hidden eligibility

| Requirement | Component | Tests | Demo evidence |
|---|---|---|---|
| FR-010 normalisation | `normalize/posting.py` | `test_qualify::test_remote_states_country_comp_type`; `test_board_verification::test_t23_remote_roles_limited_to_states_are_restricted_not_nationwide` | Demo D2 |
| FR-040 requirement extraction with quotes | `extract/rules.py`, `extract/ai.py` | `test_qualify::test_structured_posting`; `test_qualify::test_benefits_section_is_not_a_requirement`; `test_ai::test_unquoted_records_dropped_and_values_sanitized` | Demo D5 |
| FR-041 hard rules with reasons | `qualify/gate.py` | `test_qualify::test_hard_failures_each_explained`; `test_qualify::test_scam_title_exclusion_location_and_pay_floor`; `test_v21_policy::test_more_than_four_years_short_on_a_required_number_rejects`; `test_v25_audit::test_director_vp_head_and_chief_titles_stay_automatic_rejections` | Today: rejection reasons. Demo D5 |
| FR-042 unknowns are flags, eligibility facts | `qualify/gate.py`, `qualify/facts.py` | `test_qualify::test_unknown_profile_facts_are_flags_not_passes`; `test_v26_discovery::test_unknown_is_not_incompatible`; `test_v26_discovery::test_decisive_unknowns_say_what_to_verify` | Today: "Check before applying". Demo D5 |
| FR-043 held employers | `qualify/gate.py` | `test_qualify::test_held_employer_is_flagged_not_rejected`; `test_v22_primary::test_conditional_and_hold_have_their_own_sections_and_never_reach_tier_a` | Demo D5 |
| FR-044 re-evaluation with history | `pipeline.py: decision_fingerprint` | `test_pipeline::test_policy_change_reevaluates_with_history`; `test_label_findings::test_rules_version_change_reevaluates_existing_decisions`; `test_ops::test_operational_settings_never_re_evaluate_jobs` | Demo D5 |
| DQ-004 pay judged on the full range | `qualify/facts.py`, `normalize/posting.py` | `test_v26_discovery::test_pay_units_tiers_and_variable_pay`; `test_v22_primary::test_aggregator_pay_is_an_estimate_not_a_rejection` | Demo D5 (unknown pay) |
| DQ-005 "Remote" is not nationwide | `normalize/posting.py: remote_scope` | `test_board_verification::test_t23_remote_roles_limited_to_states_are_restricted_not_nationwide`; `test_label_findings::test_exclusion_list_is_not_an_allowed_list` | Demo D2 |
| DQ-009, HR-002 low-confidence text to review | `extract/rules.py`, report | `test_qualify::test_unstructured_posting_is_low_confidence` | Today: "Needs review" |
| DQ-007 structured notes validated | `discovery/note.py` | `test_v25_audit::test_bad_values_are_dropped_never_stored_and_free_text_still_works` | — |
| NFR-007 plain-language reasons | `qualify/gate.py`, `qualify/scores.py` | `test_qualify::test_hard_failures_each_explained`; `test_qualify::test_good_fit_ranks_high_and_components_explain` | Demo D5-D6 |
| FR-050, FR-051 scores, tiers | `qualify/scores.py` | `test_qualify::test_good_fit_ranks_high_and_components_explain`; `test_qualify::test_tiers_require_eligible_state_and_skill_minimum_not_just_priority`; `test_v25_audit::test_the_score_stores_the_weighted_coverage_and_uses_it_for_the_tier_gate` | Today: tier sections. Demo D6 |
| FR-070 decision evidence | `gate_results`, `scores` tables, report | `test_v22_primary::test_a_rejection_records_posting_and_profile_evidence_with_confidence_and_strength`; `test_job_lifecycle::test_every_job_flows_from_source_to_analytics_and_explains_itself` (pay rejection quotes the posting) | Today: `careeros explain JOB`. Demo D8 |
| FR-074 every job explains itself | `review/explain.py`, `cli.py: cmd_explain` | `test_job_lifecycle::test_every_job_flows_from_source_to_analytics_and_explains_itself`; `..::test_explaining_a_job_changes_nothing`; `..::test_the_explain_command_prints_text_and_json` | Today: `careeros explain JOB [--json]`. Demo D8 |
| EXC-008 no silently parked jobs | `pipeline.py: _record_review, _reevaluate, close_open_jobs` | `test_job_lifecycle::test_a_job_parked_for_review_is_rechecked_when_the_rules_change`; `..::test_a_review_parked_before_review_decisions_were_recorded_is_rechecked_once`; `..::test_a_job_that_cannot_close_is_recorded_never_silent`; `..::test_no_state_lacks_a_way_forward_and_dead_transitions_are_gone` | `docs/job_lifecycle.md` |
| NFR-003 repeat processing changes nothing | `pipeline.py` fingerprints | `test_job_lifecycle::test_seeing_and_processing_the_same_postings_again_changes_nothing`; `test_job_lifecycle::test_a_job_the_title_screen_now_rejects_is_not_sent_back_on_every_run` | Demo: `test_demo_mode::test_repeating_the_tour_gives_the_same_result` |

## BP-4 Unreliable sources

| Requirement | Component | Tests | Demo evidence |
|---|---|---|---|
| FR-030 stable employer records, no name merges | `discovery/board_registry.py` | `test_board_verification::test_t28_an_acquired_name_is_kept_as_history_and_similar_names_never_merge`; `test_board_verification::test_t27_two_legitimate_boards_belong_to_one_employer` | Demo D4 |
| FR-031 discovery = candidates only | `discovery/board_discovery.py`, `discovery_queue.py` | `test_v26_discovery::test_a_matching_title_never_verifies_the_employer`; `test_board_verification::test_t04_a_guessed_token_never_linked_by_the_employer_stays_a_candidate`; `test_board_verification::test_new_boards_from_any_path_wait_for_verification` | Today: `careeros sources list`. Demo D4 |
| FR-032 official-site handshake, trust, quarantine, validation | `discovery/board_verifier.py`, `safe_fetch.py`, `site_inspect.py` | `test_board_verification::test_t01_official_site_linking_greenhouse_confirms_handshake_identity_and_enters_quarantine`; `..::test_t10_a_high_score_without_the_official_handshake_is_never_admitted`; `..::test_t13_...`; `..::test_t14_...`; `..::test_t15_...` | Today: `careeros sources explain MAPPING`. Demo D4 |
| FR-033 three separate scores | `board_verifier.py`, `employer_intel.py`, `source_health.py` | `test_board_verification::test_t09_no_relevant_roles_lowers_career_fit_never_trust`; `..::test_t26_a_temporarily_unavailable_board_loses_health_not_trust`; `..::test_t30_repeated_relevant_hiring_raises_career_fit_and_scan_priority_not_trust` | Today: `careeros sources health` |
| FR-034 re-verification, pauses, migration, provider outages | `board_verifier.py`, `cli.py: _settle_scan_failures` | `test_board_verification::test_t07_...`; `..::test_t16_...`; `..::test_t17_...`; `..::test_t18_...`; `..::test_t21_...` | Today: "Paused boards", provider notes on the review page |
| FR-035 manual-only systems | `site_inspect.py`, `board_urls.py` | `test_board_verification::test_t03_search_found_greenhouse_but_the_official_site_uses_workday`; `..::test_t06_an_icims_careers_site_is_manual_only` | Today: "Manual-only sources". Demo D4 |
| FR-036 agencies and excluded employers never guessed | `board_discovery.py` | `test_v26_discovery::test_staffing_agencies_and_undisclosed_clients_are_never_guessed`; `..::test_excluded_and_held_employers_are_skipped` | — |
| FR-041 scam signals | `qualify/gate.py` | `test_qualify::test_scam_title_exclusion_location_and_pay_floor` | Demo D5 (scam posting) |
| DQ-006 stale flag | `review/report.py` | `test_v25_audit::test_the_stale_flag_follows_the_policy_setting`; `test_qualify::test_stale_posting_scores_lower_priority` | Today: "may be stale" |
| DQ-010 board-link normalisation | `site_inspect.py: board_of, board_key`, `board_registry.py: ensure_mapping`, `normalize/posting.py: canonical_url` | `test_board_verification::test_board_links_normalise_to_one_board_and_distinct_boards_never_merge`; `..::test_the_observed_link_and_the_canonical_board_link_are_both_kept` | Today: `careeros sources explain` (observed vs canonical link) |
| FR-034 legacy boards without proof | `board_verifier.py: _not_proven, _claim_not_shown` | `test_board_verification::test_a_legacy_board_without_proof_is_verification_required_and_keeps_its_history`; `..::test_your_unexpired_approval_survives_a_reverification_that_finds_no_link` | Today: "Legacy boards not yet proven" line |
| FR-031 aggregators discover, never verify | `board_discovery.py`, `board_verifier.py` | `test_board_verification::test_an_aggregator_lead_can_discover_a_board_but_never_verify_it` | Demo D4 |
| DQ-008 identity drift | `board_registry.py: check_drift` | `test_board_verification::test_t18_a_changed_board_fingerprint_pauses_before_anything_is_ingested` | Demo D4 |
| FR-071 explain a board | `board_verifier.py: explain` | `test_board_verification::test_a_legacy_board_verified_at_its_own_site_becomes_active_without_quarantine` | Today: `careeros sources explain` |

## BP-5 Misleading titles

| Requirement | Component | Tests | Demo evidence |
|---|---|---|---|
| FR-011 families, generic titles | `normalize/posting.py`, `qualify/gate.py` | `test_qualify::test_family_and_exclusions`; `test_v22_primary::test_generic_titles_need_context`; `test_v25_audit::test_inverted_titles_and_adjacent_families` | Demo D2 |
| FR-012 second review of title rejects | `qualify/title_rescue.py`, `pipeline.py` | `test_v26_discovery::test_relevant_ambiguous_titles_are_rescued_within_budget_and_recorded`; `..::test_the_company_blurb_never_rescues_an_unrelated_role`; `..::test_a_rule_change_can_take_a_rescue_back` | Today: rescued jobs; `careeros audit titles` |
| HR-005 title and match audits | `review/title_audit.py` | `test_v26_discovery::test_enrichment_is_kept_apart_from_new_opportunities` | Today: `careeros audit titles` |

## BP-6 Application conflicts

| Requirement | Component | Tests | Demo evidence |
|---|---|---|---|
| FR-062 one application per role, workflow, follow-up | `review/applications.py`, `config/workflows/application.json` | `test_applications::test_one_application_per_role_across_variants_and_duplicates`; `..::test_status_follows_the_matrix_and_history_stays_consistent`; `..::test_applications_section_offers_only_allowed_next_states_and_nudges_after_14_days` | Today: "Your applications" |
| FR-053 shortlist, next action, dismissal | `review/shortlist.py`, `review/dismiss.py` | `test_v26_discovery::test_shortlist_next_action_dismissal_and_factual_drafts` | Today: `careeros shortlist`. Demo D9 |
| FR-063 factual drafts | `review/shortlist.py: draft` | `test_v26_discovery::test_shortlist_next_action_dismissal_and_factual_drafts` | Today: `careeros draft JOB` |
| HR-006 nothing applied automatically | whole system (no submission code exists) | by design: no component submits applications or sends messages | — |
| HR-007 conflict of interest, holds | `qualify/night.py`, `qualify/gate.py` | `test_v25_audit::test_concrete_reasons_require_manual_review`; `test_qualify::test_held_employer_is_flagged_not_rejected` | Demo D7 |

## BP-7 Privacy

| Requirement | Component | Tests | Demo evidence |
|---|---|---|---|
| PRIV-001 vault outside the repo | `paths.py` | `test_pipeline::test_vault_cannot_live_inside_the_repo`; `..::test_gitignore_blocks_private_artifacts` | Demo: its own throwaway vault |
| PRIV-008 analytics export placement | `cli.py: cmd_export`, `analytics/export.py: synthetic_only, export` | `test_analytics::test_a_private_export_never_goes_into_the_code_repository`; `..::test_a_demo_export_stays_in_the_demo_vault`; `..::test_an_export_never_replaces_a_folder_it_did_not_write` | `samples/analytics_demo/manifest.json` (`source: demo`) |
| PRIV-007 demo data isolation | `demo/sandbox.py: demo_vault`, `paths.py: vault_dir`, `demo/runner.py: init, reset` | `test_demo_mode::test_private_vault_untouched_and_nothing_from_it_in_the_demo`; `..::test_the_private_vault_cannot_be_located_in_demo_mode`; `..::test_the_demo_vault_must_be_separate_from_the_private_vault_and_the_code`; `..::test_reset_and_init_only_touch_marked_demo_vaults` | `careeros demo tour` (vault path printed) |
| SEC-010 Demo Mode reaches nothing private | `demo/sandbox.py: transport, resolver, refuse`; guards in `http.py`, `safe_fetch.py`, `credentials.py`, `gmail.py`, `imap.py`, `github_inbox.py`, `ai.py`, `notify.py`, `cli.py` | `test_demo_mode::test_nothing_reached_the_real_network_programs_credentials_or_mail`; `..::test_every_request_went_to_the_synthetic_world`; `..::test_without_a_synthetic_world_clients_fail_instead_of_reaching_the_internet`; `..::test_integrations_with_private_sources_refuse`; `..::test_commands_that_read_private_sources_refuse`; `..::test_the_daily_run_skips_mail_and_the_github_inbox`; `..::test_the_seen_index_is_kept_locally_and_never_published` | `docs/demo_mode.md` section 3 |
| SEC-001 no secrets in the repo | `core/credentials.py`, `tools/release_scan.py` | `test_pipeline::test_repository_has_no_secrets_or_personal_contact_data`; `test_gmail::test_connect_stores_only_in_the_credential_store`; `test_release_scan::test_this_repository_holds_no_secret`; `..::test_no_value_is_ever_printed` | `tools/release_scan.py --history` (`public_release_checklist.md`) |
| PRIV-002 AI sees public text only | `extract/ai.py` | `test_ai::test_request_shape_and_only_public_posting_is_sent` | — |
| PRIV-003 published data = job facts only | `seen_index.py`, `github_inbox.py` | `test_v25_audit::test_job_facts_only`; `test_github_inbox::test_issue_is_graded_answered_closed_and_never_reread` | — |
| FR-073 sanitised seen-jobs index | `discovery/seen_index.py` | `test_v25_audit::test_job_facts_only`; `..::test_rebuilds_are_identical_and_publishing_is_idempotent` | — |
| PRIV-004 redacted logs | `core/redact.py` | `test_core::test_personal_data_removed_ids_kept` | — |
| PRIV-005 neutral exclusion reason | `qualify/gate.py` | `test_v21_policy::test_excluded_employers_are_rejected_with_a_neutral_reason` | — |
| PRIV-006 notifications | `ops/notify.py` | `test_ops::test_toast_passes_text_only_through_the_environment` | — |
| SEC-007 read-only email | `gmail.py`, `imap.py` | `test_gmail::test_read_only_scope_must_be_granted`; `test_imap::test_mailbox_is_opened_read_only_and_messages_are_never_marked_read` | — |
| SEC-002 review server | `review/server.py` | `test_pipeline::test_token_host_and_form_key_required` | Today: `careeros review` |
| SEC-003 untrusted content is data | `review/report.py`, `site_inspect.py`, `note.py` | `test_pipeline::test_report_escapes_posting_text_and_labels_measure_agreement`; `test_board_verification::test_t22_instructions_written_into_a_careers_page_are_only_data`; `test_v26_discovery::test_external_text_cannot_point_discovery_at_other_hosts_or_change_policy` | Demo D4 |
| SEC-004, SEC-005 safe fetching | `safe_fetch.py`, `http.py` | `test_board_verification::test_t19_...`; `..::test_t20_...`; `..::test_urls_that_are_never_fetched`; `..::test_connections_are_pinned_to_the_checked_address`; `..::test_the_daily_scan_never_follows_a_board_redirect_off_the_listing_apis`; `test_v26_discovery::test_redirects_outside_the_allowlist_are_refused` | — |
| SEC-008 never-automated sites | `safe_fetch.py: NEVER_FETCH`, `sources.py` | `test_board_verification::test_a_job_site_or_a_never_automated_site_is_not_an_official_domain`; `test_config::test_linkedin_indeed_and_glassdoor_are_never_automated`; `..::test_no_site_allows_automated_filling_or_submitting` | — |
| SEC-009 approvals cannot bypass hard failures | `board_registry.py: approve` | `test_board_verification::test_manual_only_and_hard_failures_cannot_be_approved`; `..::test_t21_...` | — |
| HR-004 expiring approvals | `board_registry.py: approve, expire_overrides` | `test_board_verification::test_t25_an_expired_manual_override_returns_the_board_to_verification` | — |

## BP-8 Feedback loop and control

| Requirement | Component | Tests | Demo evidence |
|---|---|---|---|
| FR-061, HR-001 labels and agreement | `review/labels.py`, `cli.py: cmd_eval` | `test_pipeline::test_report_escapes_posting_text_and_labels_measure_agreement`; `test_saved::test_review_page_labels_through_the_browser_form` | Today: `careeros eval` |
| FR-075 analytics export (star schema, validated) | `analytics/export.py`, `cli.py: cmd_export`, `tools/build_demo_export.py` | `test_analytics::test_the_demo_export_is_valid_synthetic_and_reconciles_with_the_application`; `..::test_validation_catches_broken_exports`; `..::test_the_committed_sample_is_exactly_this_export`; `..::test_every_table_and_column_is_documented_in_order`; `..::test_every_kpi_value_quoted_in_the_dashboard_spec_is_measured`; `..::test_the_export_only_reads_and_repeats_exactly`; `..::test_rows_marked_as_drills_or_tests_are_left_out`; `test_powerbi::test_the_committed_project_is_exactly_what_the_generator_builds`; `..::test_every_reference_resolves`; `..::test_relationships_follow_the_exports_references`; `..::test_the_measures_are_those_of_the_dashboard_spec` | Demo D10: `samples/analytics_demo/`, `powerbi/CareerOS.pbix`, `powerbi/screenshots/` |
| FR-072 metrics and funnel | `review/discovery_metrics.py`, `review/funnel.py` | `test_v26_discovery::test_enrichment_is_kept_apart_from_new_opportunities`; `test_v25_audit::test_recommended_to_hired_with_negatives_and_new_dimensions`; `test_demo_mode::test_reported_verified_boards_are_the_boards_verification_admitted` | Today: `careeros metrics`, `careeros funnel`. Demo D10 (M5 export) |
| FR-060 review page | `review/report.py`, `review/server.py` | `test_applications::test_new_since_last_review_and_stale_flag`; `test_pipeline::test_full_flow` | Today: `careeros review`. Demo D9 |
| AUD-001 transitions and fail-closed | `db/store.py`, `core/transitions.py` | `test_core::test_transition_writes_history_projection_and_audit`; `..::test_illegal_transition_leaves_no_trace`; `..::test_projection_mismatch_fails_closed` | Today: `careeros verify` |
| AUD-002, FR-081 hash chain and on-demand verification | `db/store.py`, `cli.py: cmd_verify` | `test_core::test_event_chain_detects_tampering`; `test_core::test_history_invariants`; `test_applications::test_status_follows_the_matrix_and_history_stays_consistent` | Today: `careeros verify` |
| NFR-001 local, single user | whole system | by design (SQLite file, no server) | — |
| NFR-006 portability | platform guards in `core/credentials.py`, `core/locks.py`, `ops/notify.py` | CI runs Windows (required) and Ubuntu (informational): `test_ci::test_windows_is_required_and_linux_informational`. Neither has run on GitHub yet (no remote); Linux is unverified | — |
| NFR-008 policy configuration | `core/policy.py`, `pipeline.py: NOT_PRIMARY_DECISIONS` | `test_ops::test_operational_settings_never_re_evaluate_jobs` | — |
| AUD-003 append-only history | schema triggers | `test_core::test_history_tables_are_immutable` | — |
| AUD-004 idempotency | `db/store.py: receipt` | `test_core::test_idempotency_receipts`; `test_applications::test_record_once_and_replay_a_double_click` | — |
| AUD-005 verification evidence chain | `board_verifier.py: RunLog` | `test_board_verification::test_t01_...` (evidence types asserted) | Today: `careeros sources explain` |
| AUD-006 drills excluded from metrics | `review/*`, `review/discovery_metrics.py`, `analytics/export.py` | `test_pipeline::test_excluded_fixture_rows_never_count_in_metrics`; `test_analytics::test_rows_marked_as_drills_or_tests_are_left_out` | — |
| SEC-006 separation of duties | `db/store.py` | `test_core::test_separation_of_duties`; `..::test_state_columns_only_via_transition` | — |
| NFR-002, EXC-001 per-item failure isolation | `cli.py`, `pipeline.py` | `test_v25_audit::test_one_malformed_github_row_does_not_kill_the_batch`; `..::test_one_employer_board_failing_unexpectedly_never_stops_the_scan`; `..::test_one_failing_night_assessment_never_stops_the_others`; `test_ops::test_a_failed_daily_run_shows_a_notification` | — |
| EXC-002, EXC-003 board and provider failures | `cli.py: _settle_scan_failures` | `test_v26_discovery::test_access_restriction_disables_only_after_repeats`; `test_board_verification::test_t16_...` | Today: review page notes |
| EXC-004 lock | `core/locks.py` | `test_core::test_second_writer_refused_and_stale_lock_held` | — |
| EXC-005 email outage lookback | `seen_index.py` | `test_v25_audit::test_a_reported_outage_asks_the_next_run_for_a_seven_day_lookback_without_any_gmail_data` | — |
| EXC-006 inbox and email retries | `github_inbox.py`, `alerts.py` | `test_github_inbox::test_failed_close_is_retried_next_run`; `test_alerts::test_unreadable_email_is_set_aside_with_an_exception` | — |
| EXC-007 unreadable employer site | `board_verifier.py: _site_failure` | `test_board_verification::test_an_inconclusive_legacy_check_asks_nothing_of_you_until_a_domain_is_given` | — |
| HR-003 review cases | `board_registry.py: open_case` | `test_board_verification::test_site_checks_off_means_no_site_request_and_a_review_case`; `..::test_t05_...` | Today: "Boards needing your review" |
| FR-080 backups, notification | `ops/backup.py`, `ops/notify.py` | `test_ops::test_backup_is_complete_verified_and_replaces_the_same_day`; `..::test_summary_counts_only_new_jobs_worth_a_look_and_relevant_leads` | — |
| NFR-003 reproducible decisions | `pipeline.py: decision_fingerprint` | `test_ops::test_operational_settings_never_re_evaluate_jobs`; `..::test_night_settings_re_check_night_only`; `test_demo_mode::test_the_tour_produces_exactly_the_recorded_result` | Demo: two tours, same result |
| NFR-004 quality gate | `tests/`, `tools/mutate.py`, `tools/validate.py`, `.github/workflows/ci.yml` | the suite itself; mutation receipt `evidence/mutation-receipt.json`; `test_ci::test_it_installs_the_hash_locked_tools_and_runs_validate`; `..::test_a_new_type_error_fails_a_moved_one_does_not_and_fixed_ones_are_counted`; `..::test_only_loopback_can_be_resolved_or_reached` | Demo D11 (M3/M6); `tools/validate.py` |

## BP-9 Second job search (overnight / part-time)

| Requirement | Component | Tests | Demo evidence |
|---|---|---|---|
| FR-052 NIGHT routing | `qualify/night.py`, `night_results` table | `test_v22_night::test_mode_rule`; `..::test_each_mode_uses_its_own_floor_and_aggregator_pay_never_rejects`; `..::test_results_are_separate_idempotent_and_off_by_default`; `..::test_night_only_jobs_stay_out_of_primary_rejection_lists`; `test_v25_audit::test_workload_is_informational_but_overlapping_hours_make_supplemental_incompatible` | Today: NIGHT section. Demo D7 |
| schedule reading | `qualify/night.py` | `test_v22_night::test_only_schedule_phrases_count`; `..::test_business_or_training_hours_are_not_proof_of_a_day_job` | Demo D7 |

## Demo script steps
`careeros demo tour`, then the listed commands with `CAREEROS_DEMO_MODE=true`. Details: `docs/demo_mode.md`.

| Step | What the reviewer sees | Status |
|---|---|---|
| D1 | Synthetic sources load: fake job boards, an alert email and a pasted posting. No network, no credentials. | Exists (day 1). The recommendation inbox is refused in Demo Mode rather than faked. |
| D2 | Normalised fields for a few postings (remote scope, states, pay annualised, family). | Exists: `careeros explain JOB` |
| D3 | Duplicate, variant and repost handling; a closure only after missed complete scans. | Exists: duplicate (Meridian 101 / 102), closure (Meridian 103), board retirement (Harborline) |
| D4 | Board verification: one verified, one contradicted by the employer's own site, one same-name impostor, one manual-only. | Exists, except the contradicted case: `careeros sources health`, `sources explain` |
| D5 | Eligibility: an eligible job, a hard-rule rejection with a quote, an unknown-pay job, a scam. | Exists |
| D6 | Scores and tiers with component breakdowns. | Exists: `careeros explain JOB`, the report |
| D7 | NIGHT routing of an overnight posting. | Partly: 13 daytime roles are routed to NIGHT as conditional; the overnight posting is judged as Replacement work and rejected for pay. No overnight posting passes NIGHT yet. |
| D8 | `careeros explain JOB`: the full evidence for one decision. | Exists |
| D9 | The review page. | Exists: `careeros review` |
| D10 | Analytics export for Power BI. | Exists: `careeros export` with Demo Mode on, committed as `samples/analytics_demo/`; the report `powerbi/CareerOS.pbip` (the private build also keeps a `.pbix`) (five pages, checked in Power BI Desktop) and its screenshots. |
| D11 | Validation: tests and the release-safety scan. | Tests exist (`tests/test_demo_mode.py`); the release-safety scan is Milestone 6 |

## Coverage summary
- **Requirements:** 101 (FR 41, NFR 8, DQ 10, SEC 10, PRIV 8, AUD 6, PERF 3, EXC 8, HR 7), plus 12 acceptance criteria.
  Every one appears in this matrix.
- **With at least one automated test:** all FR / DQ / SEC / PRIV / AUD / EXC / HR requirements, except:
  - FR-080 scheduling (Windows Task Scheduler is external; backup and notification are tested);
  - HR-006, satisfied by design: no submission code exists.
- **Non-functional:** 5 of 8 tested.
  - NFR-001: by design.
  - NFR-004: is the test gate itself.
  - NFR-006 (portability): checked by CI on Linux (an informational job, passing since 2026-10-15), not by a test of
    its own.
- **Measured, not tested:** PERF-001 (run duration). PERF-003 is observed.
- **Open:**
  - demo step D4 lacks a board contradicted by the employer's own site, and D7 lacks an overnight posting that NIGHT
    accepts (`functional_mvp_report.md`);
  - AC-004 (50 labels) is pending the job seeker's labels.
