-- Frozen database fixture for v0.2.0.
--
-- Produced by scripts/make_upgrade_fixture.py. NEVER EDIT THIS FILE.
-- Every future migration must be able to upgrade it; that is the only
-- job it has. If a migration cannot, the migration is wrong.
--
--
-- PostgreSQL database dump
--

\restrict OFGPPwKEIbCbIzKrlkCi5cdRprtdalnSLnwFZ9yDO9lXcZiBYD5UXHQRjKoGsV2

-- Dumped from database version 16.13
-- Dumped by pg_dump version 16.13

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: account_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE "public"."account_kind" AS ENUM (
    'user_available',
    'user_hold',
    'hot_wallet',
    'payouts_in_flight',
    'fee_income',
    'network_fee_expense',
    'external',
    'user_deficit'
);


--
-- Name: deposit_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE "public"."deposit_status" AS ENUM (
    'creating',
    'pending',
    'confirming',
    'settled',
    'expired',
    'review',
    'dismissed',
    'failed'
);


--
-- Name: entry_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE "public"."entry_kind" AS ENUM (
    'deposit_credit',
    'withdrawal_hold',
    'withdrawal_settle',
    'withdrawal_release',
    'adjustment',
    'reversal',
    'withdrawal_submit'
);


--
-- Name: withdrawal_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE "public"."withdrawal_status" AS ENUM (
    'requested',
    'pending_approval',
    'approved',
    'rejected',
    'submitting',
    'submitted',
    'broadcast',
    'confirmed',
    'failed',
    'refunded'
);


--
-- Name: assert_entry_balanced(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."assert_entry_balanced"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
        DECLARE
            entry_total BIGINT;
        BEGIN
            SELECT COALESCE(SUM(amount), 0) INTO entry_total
                FROM postings WHERE entry_id = NEW.entry_id;
            IF entry_total <> 0 THEN
                RAISE EXCEPTION 'journal entry % does not balance: sum = %',
                    NEW.entry_id, entry_total
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: reject_ledger_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."reject_ledger_mutation"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed, post a reversal entry instead',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$;


SET default_tablespace = '';

SET default_table_access_method = "heap";

--
-- Name: accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."accounts" (
    "id" bigint NOT NULL,
    "asset_id" "text" NOT NULL,
    "kind" "public"."account_kind" NOT NULL,
    "external_user_id" "text",
    "normal_side" "text" NOT NULL,
    "balance" bigint DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "accounts_normal_side" CHECK (("normal_side" = ANY (ARRAY['debit'::"text", 'credit'::"text"]))),
    CONSTRAINT "no_negative_asset" CHECK ((("kind" = ANY (ARRAY['external'::"public"."account_kind", 'user_deficit'::"public"."account_kind"])) OR ("normal_side" <> 'debit'::"text") OR ("balance" >= 0))),
    CONSTRAINT "no_overdraft" CHECK ((("kind" = ANY (ARRAY['external'::"public"."account_kind", 'user_deficit'::"public"."account_kind"])) OR ("normal_side" <> 'credit'::"text") OR ("balance" <= 0))),
    CONSTRAINT "user_acct_has_user" CHECK ((("kind" = ANY (ARRAY['user_available'::"public"."account_kind", 'user_hold'::"public"."account_kind"])) = ("external_user_id" IS NOT NULL)))
);


--
-- Name: accounts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE "public"."accounts" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."accounts_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."alembic_version" (
    "version_num" character varying(32) NOT NULL
);


--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."api_keys" (
    "id" bigint NOT NULL,
    "key_id" "text" NOT NULL,
    "key_hash" "text" NOT NULL,
    "name" "text" NOT NULL,
    "scope" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "expires_at" timestamp with time zone,
    "revoked_at" timestamp with time zone,
    "last_used_at" timestamp with time zone,
    CONSTRAINT "api_keys_scope" CHECK (("scope" = ANY (ARRAY['readwrite'::"text", 'admin'::"text"])))
);


--
-- Name: api_keys_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE "public"."api_keys" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."api_keys_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."assets" (
    "id" "text" NOT NULL,
    "display_name" "text" NOT NULL,
    "decimals" smallint NOT NULL,
    "unit_name" "text" NOT NULL,
    "btcpay_payment_method" "text" NOT NULL,
    "withdrawal_auto_limit" bigint NOT NULL,
    "withdrawal_daily_cap" bigint NOT NULL,
    "withdrawal_user_daily_cap" bigint,
    "withdrawal_min" bigint DEFAULT 1 NOT NULL,
    "withdrawal_flat_fee" bigint DEFAULT 0 NOT NULL,
    "enabled" boolean DEFAULT true NOT NULL,
    "pooled_addresses" boolean DEFAULT false NOT NULL,
    "invoice_currency" "text",
    "deposit_expiry_minutes" integer,
    CONSTRAINT "assets_decimals_range" CHECK ((("decimals" >= 0) AND ("decimals" <= 8))),
    CONSTRAINT "assets_expiry_positive" CHECK ((("deposit_expiry_minutes" IS NULL) OR ("deposit_expiry_minutes" > 0)))
);


--
-- Name: deposit_payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."deposit_payments" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "deposit_id" "uuid" NOT NULL,
    "btcpay_payment_id" "text" NOT NULL,
    "amount" bigint NOT NULL,
    "after_expiration" boolean DEFAULT false NOT NULL,
    "ledger_entry_id" bigint,
    "credited_at" timestamp with time zone,
    "resolved_by" "text",
    CONSTRAINT "deposit_payments_amount_positive" CHECK (("amount" > 0))
);


--
-- Name: deposits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."deposits" (
    "id" "uuid" NOT NULL,
    "external_user_id" "text" NOT NULL,
    "asset_id" "text" NOT NULL,
    "btcpay_invoice_id" "text",
    "amount_expected" bigint,
    "amount_credited" bigint DEFAULT 0 NOT NULL,
    "status" "public"."deposit_status" DEFAULT 'creating'::"public"."deposit_status" NOT NULL,
    "address" "text",
    "checkout_link" "text",
    "expires_at" timestamp with time zone,
    "last_payment_seen_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "monitoring_expires_at" timestamp with time zone,
    "address_reserved_from" timestamp with time zone,
    "address_reserved_until" timestamp with time zone
);


--
-- Name: idempotency_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."idempotency_keys" (
    "key" "text" NOT NULL,
    "endpoint" "text" NOT NULL,
    "request_hash" "text" NOT NULL,
    "state" "text" DEFAULT 'in_progress'::"text" NOT NULL,
    "resource_id" "text",
    "response_status" smallint,
    "response_body" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "idempotency_keys_state" CHECK (("state" = ANY (ARRAY['in_progress'::"text", 'completed'::"text"])))
);


--
-- Name: journal_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."journal_entries" (
    "id" bigint NOT NULL,
    "kind" "public"."entry_kind" NOT NULL,
    "asset_id" "text" NOT NULL,
    "source_ref" "text" NOT NULL,
    "reverses_entry_id" bigint,
    "memo" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: journal_entries_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE "public"."journal_entries" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."journal_entries_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: outbound_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."outbound_events" (
    "id" "uuid" NOT NULL,
    "event_type" "text" NOT NULL,
    "payload" "jsonb" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "attempts" smallint DEFAULT 0 NOT NULL,
    "next_attempt_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "last_error" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "outbound_events_status" CHECK (("status" = ANY (ARRAY['pending'::"text", 'delivered'::"text", 'dead'::"text"])))
);


--
-- Name: postings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."postings" (
    "id" bigint NOT NULL,
    "entry_id" bigint NOT NULL,
    "account_id" bigint NOT NULL,
    "amount" bigint NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "postings_amount_nonzero" CHECK (("amount" <> 0))
);


--
-- Name: postings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE "public"."postings" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."postings_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: wallet_txo_alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."wallet_txo_alerts" (
    "id" bigint NOT NULL,
    "asset_id" "text" NOT NULL,
    "txid" "text" NOT NULL,
    "amount" bigint NOT NULL,
    "confirmations" integer,
    "status" "text" DEFAULT 'open'::"text" NOT NULL,
    "note" "text",
    "detected_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "resolved_at" timestamp with time zone,
    "resolved_by" "text",
    CONSTRAINT "wallet_txo_alerts_status" CHECK (("status" = ANY (ARRAY['open'::"text", 'resolved'::"text", 'ignored'::"text"])))
);


--
-- Name: wallet_txo_alerts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE "public"."wallet_txo_alerts" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."wallet_txo_alerts_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: webhook_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."webhook_events" (
    "id" bigint NOT NULL,
    "dedup_key" "text" NOT NULL,
    "delivery_id" "text" NOT NULL,
    "event_type" "text" NOT NULL,
    "btcpay_invoice_id" "text",
    "btcpay_payout_id" "text",
    "payload" "jsonb" NOT NULL,
    "status" "text" DEFAULT 'received'::"text" NOT NULL,
    "received_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "processed_at" timestamp with time zone,
    "attempts" smallint DEFAULT 0 NOT NULL,
    "processing_error" "text",
    CONSTRAINT "webhook_events_status" CHECK (("status" = ANY (ARRAY['received'::"text", 'processed'::"text", 'failed'::"text", 'ignored'::"text", 'orphaned'::"text"])))
);


--
-- Name: webhook_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE "public"."webhook_events" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."webhook_events_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: withdrawals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."withdrawals" (
    "id" "uuid" NOT NULL,
    "external_user_id" "text" NOT NULL,
    "asset_id" "text" NOT NULL,
    "destination_address" "text" NOT NULL,
    "amount_gross" bigint NOT NULL,
    "fee_amount" bigint,
    "amount_net" bigint,
    "status" "public"."withdrawal_status" DEFAULT 'requested'::"public"."withdrawal_status" NOT NULL,
    "approval_mode" "text",
    "approved_by" "text",
    "backend" "text" NOT NULL,
    "backend_ref" "text",
    "txid" "text",
    "hold_entry_id" bigint,
    "settle_entry_id" bigint,
    "failure_reason" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "submit_entry_id" bigint,
    "release_entry_id" bigint,
    "submitted_at" timestamp with time zone,
    "rejected_by" "text",
    "released_by" "text",
    "release_attestation" "text",
    CONSTRAINT "withdrawals_approval_mode" CHECK (("approval_mode" = ANY (ARRAY['auto'::"text", 'manual'::"text"]))),
    CONSTRAINT "withdrawals_gross_positive" CHECK (("amount_gross" > 0))
);


--
-- Name: worker_heartbeats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."worker_heartbeats" (
    "job_name" "text" NOT NULL,
    "last_run_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "last_result" "text",
    "last_error" "text"
);


--
-- Data for Name: accounts; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (5, 'BTC', 'external', NULL, 'credit', 0, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (6, 'BTC', 'user_deficit', NULL, 'debit', 0, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (8, 'USDT_TRC20', 'payouts_in_flight', NULL, 'debit', 0, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (9, 'USDT_TRC20', 'fee_income', NULL, 'credit', 0, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (10, 'USDT_TRC20', 'network_fee_expense', NULL, 'debit', 0, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (11, 'USDT_TRC20', 'external', NULL, 'credit', 0, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (12, 'USDT_TRC20', 'user_deficit', NULL, 'debit', 0, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (7, 'USDT_TRC20', 'hot_wallet', NULL, 'debit', 900000000, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (13, 'BTC', 'user_available', 'fixture-user-a', 'credit', -490000000, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (1, 'BTC', 'hot_wallet', NULL, 'debit', 610000000, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (2, 'BTC', 'payouts_in_flight', NULL, 'debit', 0, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (3, 'BTC', 'fee_income', NULL, 'credit', -30000, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (4, 'BTC', 'network_fee_expense', NULL, 'debit', 30000, '2026-08-11 18:06:49.932036+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (14, 'BTC', 'user_hold', 'fixture-user-a', 'credit', 0, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (15, 'BTC', 'user_available', 'fixture-user-b', 'credit', -60000000, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (16, 'BTC', 'user_hold', 'fixture-user-b', 'credit', -60000000, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (17, 'USDT_TRC20', 'user_available', 'fixture-user-a', 'credit', -700000000, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."accounts" OVERRIDING SYSTEM VALUE VALUES (18, 'USDT_TRC20', 'user_hold', 'fixture-user-a', 'credit', -200000000, '2026-08-11 18:06:49.972917+00');


--
-- Data for Name: alembic_version; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."alembic_version" VALUES ('0006');


--
-- Data for Name: api_keys; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: assets; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."assets" VALUES ('BTC', 'Bitcoin', 8, 'sat', 'BTC-CHAIN', 500000, 5000000, NULL, 1, 0, true, false, 'BTC', NULL);
INSERT INTO "public"."assets" VALUES ('USDT_TRC20', 'Tether USD (TRC-20)', 6, 'microUSDT', 'USDT_TRC20', 200000000, 2000000000, NULL, 1, 1000000, true, true, 'USDT', NULL);


--
-- Data for Name: deposit_payments; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."deposit_payments" VALUES ('019fe000-0000-7000-8000-0000000000a1', '019fe000-0000-7000-8000-00000000d001', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-0', 500000000, false, NULL, '2026-08-10 12:00:00+00', 'auto');


--
-- Data for Name: deposits; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."deposits" VALUES ('019fe000-0000-7000-8000-00000000d001', 'fixture-user-a', 'BTC', 'fixture-invoice-1', 500000000, 500000000, 'settled', 'bcrt1qakxe0syt7wkayqgccadyayhtfxhk9mvmvtq0sx', 'https://btcpay.example/i/fixture-invoice-1', '2026-08-10 13:00:00+00', NULL, '2026-08-11 18:06:50.02498+00', '2026-08-11 18:06:50.02498+00', '2026-08-11 12:00:00+00', '2026-08-10 12:00:00+00', '2026-08-11 12:00:00+00');


--
-- Data for Name: idempotency_keys; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: journal_entries; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."journal_entries" OVERRIDING SYSTEM VALUE VALUES (1, 'deposit_credit', 'BTC', 'btcpay_payment:fixture-fixture-user-a:0', NULL, NULL, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."journal_entries" OVERRIDING SYSTEM VALUE VALUES (2, 'deposit_credit', 'BTC', 'btcpay_payment:fixture-fixture-user-b:0', NULL, NULL, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."journal_entries" OVERRIDING SYSTEM VALUE VALUES (3, 'deposit_credit', 'USDT_TRC20', 'btcpay_payment:fixture-usdt:0', NULL, NULL, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."journal_entries" OVERRIDING SYSTEM VALUE VALUES (4, 'withdrawal_hold', 'BTC', 'withdrawal_hold:019ff201-76f7-76e6-b7f7-049e4d829902', NULL, 'withdrawal 019ff201-76f7-76e6-b7f7-049e4d829902', '2026-08-11 18:06:50.02498+00');
INSERT INTO "public"."journal_entries" OVERRIDING SYSTEM VALUE VALUES (5, 'withdrawal_submit', 'BTC', 'withdrawal_submit:019ff201-76f7-76e6-b7f7-049e4d829902', NULL, 'withdrawal 019ff201-76f7-76e6-b7f7-049e4d829902 committed to a payout', '2026-08-11 18:06:50.116637+00');
INSERT INTO "public"."journal_entries" OVERRIDING SYSTEM VALUE VALUES (6, 'withdrawal_settle', 'BTC', 'withdrawal_settle:019ff201-76f7-76e6-b7f7-049e4d829902', NULL, 'withdrawal 019ff201-76f7-76e6-b7f7-049e4d829902 confirmed', '2026-08-11 18:06:50.116637+00');
INSERT INTO "public"."journal_entries" OVERRIDING SYSTEM VALUE VALUES (7, 'withdrawal_hold', 'BTC', 'withdrawal_hold:019ff201-7728-7b37-a824-87ac5d41e5c2', NULL, 'withdrawal 019ff201-7728-7b37-a824-87ac5d41e5c2', '2026-08-11 18:06:50.144674+00');
INSERT INTO "public"."journal_entries" OVERRIDING SYSTEM VALUE VALUES (8, 'withdrawal_hold', 'USDT_TRC20', 'withdrawal_hold:019ff201-773a-7d65-ac78-a2e1a9bbf3f3', NULL, 'withdrawal 019ff201-773a-7d65-ac78-a2e1a9bbf3f3', '2026-08-11 18:06:50.162464+00');


--
-- Data for Name: outbound_events; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."outbound_events" VALUES ('019ff201-7701-71e7-866f-01cbc3631e25', 'withdrawal.pending_approval', '{"fee": null, "txid": null, "asset": "BTC", "reason": "above the per-withdrawal auto-approval limit; would exceed the rolling 24h cap for this asset", "status": "pending_approval", "amount_net": null, "amount_gross": "0.10000000", "withdrawal_id": "019ff201-76f7-76e6-b7f7-049e4d829902", "external_user_id": "fixture-user-a", "destination_address": "bcrt1q2fhpadugqsm3twzvpg8veawxeudcsaq7ufxxfj"}', 'pending', 0, '2026-08-11 18:06:50.02498+00', NULL, '2026-08-11 18:06:50.02498+00');
INSERT INTO "public"."outbound_events" VALUES ('019ff201-7730-7234-bd40-f27ffa2e3476', 'withdrawal.pending_approval', '{"fee": null, "txid": null, "asset": "BTC", "reason": "above the per-withdrawal auto-approval limit; would exceed the rolling 24h cap for this asset", "status": "pending_approval", "amount_net": null, "amount_gross": "0.60000000", "withdrawal_id": "019ff201-7728-7b37-a824-87ac5d41e5c2", "external_user_id": "fixture-user-b", "destination_address": "bcrt1q2fhpadugqsm3twzvpg8veawxeudcsaq7ufxxfj"}', 'pending', 0, '2026-08-11 18:06:50.144674+00', NULL, '2026-08-11 18:06:50.144674+00');
INSERT INTO "public"."outbound_events" VALUES ('019ff201-7742-71a3-9f1e-725c2a83a8da', 'withdrawal.pending_approval', '{"fee": null, "txid": null, "asset": "USDT_TRC20", "reason": "asset requires manual approval", "status": "pending_approval", "amount_net": null, "amount_gross": "200.000000", "withdrawal_id": "019ff201-773a-7d65-ac78-a2e1a9bbf3f3", "external_user_id": "fixture-user-a", "destination_address": "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"}', 'pending', 0, '2026-08-11 18:06:50.162464+00', NULL, '2026-08-11 18:06:50.162464+00');
INSERT INTO "public"."outbound_events" VALUES ('019fc000-0000-7000-8000-0000000000c1', 'deposit.settled', '{"deposit_id": "019fe000-0000-7000-8000-00000000d001", "amount_credited": "5.00000000"}', 'pending', 0, '2026-08-11 18:06:50.179804+00', NULL, '2026-08-11 18:06:50.179804+00');


--
-- Data for Name: postings; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (1, 1, 1, 500000000, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (2, 1, 13, -500000000, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (3, 2, 1, 120000000, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (4, 2, 15, -120000000, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (5, 3, 7, 900000000, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (6, 3, 17, -900000000, '2026-08-11 18:06:49.972917+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (7, 4, 13, 10000000, '2026-08-11 18:06:50.02498+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (8, 4, 14, -10000000, '2026-08-11 18:06:50.02498+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (9, 5, 2, 10000000, '2026-08-11 18:06:50.116637+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (10, 5, 1, -10000000, '2026-08-11 18:06:50.116637+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (11, 6, 14, 10000000, '2026-08-11 18:06:50.116637+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (12, 6, 2, -10000000, '2026-08-11 18:06:50.116637+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (13, 6, 4, 30000, '2026-08-11 18:06:50.116637+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (14, 6, 3, -30000, '2026-08-11 18:06:50.116637+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (15, 7, 15, 60000000, '2026-08-11 18:06:50.144674+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (16, 7, 16, -60000000, '2026-08-11 18:06:50.144674+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (17, 8, 17, 200000000, '2026-08-11 18:06:50.162464+00');
INSERT INTO "public"."postings" OVERRIDING SYSTEM VALUE VALUES (18, 8, 18, -200000000, '2026-08-11 18:06:50.162464+00');


--
-- Data for Name: wallet_txo_alerts; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: webhook_events; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."webhook_events" OVERRIDING SYSTEM VALUE VALUES (1, 'fixture-delivery-1', 'fixture-delivery-1', 'InvoiceSettled', 'fixture-invoice-1', NULL, '{"type": "InvoiceSettled", "invoiceId": "fixture-invoice-1"}', 'processed', '2026-08-11 18:06:50.179804+00', '2026-08-10 12:00:00+00', 1, NULL);


--
-- Data for Name: withdrawals; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."withdrawals" VALUES ('019ff201-76f7-76e6-b7f7-049e4d829902', 'fixture-user-a', 'BTC', 'bcrt1q2fhpadugqsm3twzvpg8veawxeudcsaq7ufxxfj', 10000000, 30000, 9970000, 'confirmed', 'manual', NULL, 'btcpay_payout', 'fixture-payout-1', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 4, 6, NULL, '2026-08-11 18:06:50.02498+00', '2026-08-11 18:06:50.02498+00', 5, NULL, '2026-08-10 12:00:00+00', NULL, NULL, NULL);
INSERT INTO "public"."withdrawals" VALUES ('019ff201-7728-7b37-a824-87ac5d41e5c2', 'fixture-user-b', 'BTC', 'bcrt1q2fhpadugqsm3twzvpg8veawxeudcsaq7ufxxfj', 60000000, NULL, NULL, 'pending_approval', 'manual', NULL, 'btcpay_payout', NULL, NULL, 7, NULL, NULL, '2026-08-11 18:06:50.144674+00', '2026-08-11 18:06:50.144674+00', NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO "public"."withdrawals" VALUES ('019ff201-773a-7d65-ac78-a2e1a9bbf3f3', 'fixture-user-a', 'USDT_TRC20', 'TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE', 200000000, NULL, NULL, 'pending_approval', 'manual', NULL, 'manual_tron', NULL, NULL, 8, NULL, NULL, '2026-08-11 18:06:50.162464+00', '2026-08-11 18:06:50.162464+00', NULL, NULL, NULL, NULL, NULL, NULL);


--
-- Data for Name: worker_heartbeats; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."worker_heartbeats" VALUES ('webhooks', '2026-08-10 12:00:00+00', 'ok', NULL);


--
-- Name: accounts_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."accounts_id_seq"', 26, true);


--
-- Name: api_keys_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."api_keys_id_seq"', 1, false);


--
-- Name: journal_entries_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."journal_entries_id_seq"', 8, true);


--
-- Name: postings_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."postings_id_seq"', 18, true);


--
-- Name: wallet_txo_alerts_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."wallet_txo_alerts_id_seq"', 1, false);


--
-- Name: webhook_events_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."webhook_events_id_seq"', 1, true);


--
-- Name: accounts accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."accounts"
    ADD CONSTRAINT "accounts_pkey" PRIMARY KEY ("id");


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."alembic_version"
    ADD CONSTRAINT "alembic_version_pkc" PRIMARY KEY ("version_num");


--
-- Name: api_keys api_keys_key_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_key_id_key" UNIQUE ("key_id");


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_pkey" PRIMARY KEY ("id");


--
-- Name: assets assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."assets"
    ADD CONSTRAINT "assets_pkey" PRIMARY KEY ("id");


--
-- Name: deposit_payments deposit_payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."deposit_payments"
    ADD CONSTRAINT "deposit_payments_pkey" PRIMARY KEY ("id");


--
-- Name: deposits deposits_btcpay_invoice_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."deposits"
    ADD CONSTRAINT "deposits_btcpay_invoice_id_key" UNIQUE ("btcpay_invoice_id");


--
-- Name: deposits deposits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."deposits"
    ADD CONSTRAINT "deposits_pkey" PRIMARY KEY ("id");


--
-- Name: idempotency_keys idempotency_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."idempotency_keys"
    ADD CONSTRAINT "idempotency_keys_pkey" PRIMARY KEY ("key", "endpoint");


--
-- Name: journal_entries journal_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."journal_entries"
    ADD CONSTRAINT "journal_entries_pkey" PRIMARY KEY ("id");


--
-- Name: outbound_events outbound_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."outbound_events"
    ADD CONSTRAINT "outbound_events_pkey" PRIMARY KEY ("id");


--
-- Name: postings postings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."postings"
    ADD CONSTRAINT "postings_pkey" PRIMARY KEY ("id");


--
-- Name: deposit_payments ux_deposit_payment; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."deposit_payments"
    ADD CONSTRAINT "ux_deposit_payment" UNIQUE ("deposit_id", "btcpay_payment_id");


--
-- Name: journal_entries ux_entry_source; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."journal_entries"
    ADD CONSTRAINT "ux_entry_source" UNIQUE ("kind", "source_ref");


--
-- Name: wallet_txo_alerts ux_wallet_txo_alert; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."wallet_txo_alerts"
    ADD CONSTRAINT "ux_wallet_txo_alert" UNIQUE ("asset_id", "txid");


--
-- Name: wallet_txo_alerts wallet_txo_alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."wallet_txo_alerts"
    ADD CONSTRAINT "wallet_txo_alerts_pkey" PRIMARY KEY ("id");


--
-- Name: webhook_events webhook_events_dedup_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."webhook_events"
    ADD CONSTRAINT "webhook_events_dedup_key_key" UNIQUE ("dedup_key");


--
-- Name: webhook_events webhook_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."webhook_events"
    ADD CONSTRAINT "webhook_events_pkey" PRIMARY KEY ("id");


--
-- Name: withdrawals withdrawals_backend_ref_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."withdrawals"
    ADD CONSTRAINT "withdrawals_backend_ref_key" UNIQUE ("backend_ref");


--
-- Name: withdrawals withdrawals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."withdrawals"
    ADD CONSTRAINT "withdrawals_pkey" PRIMARY KEY ("id");


--
-- Name: worker_heartbeats worker_heartbeats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."worker_heartbeats"
    ADD CONSTRAINT "worker_heartbeats_pkey" PRIMARY KEY ("job_name");


--
-- Name: ix_deposits_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_deposits_active" ON "public"."deposits" USING "btree" ("status") WHERE ("status" = ANY (ARRAY['creating'::"public"."deposit_status", 'pending'::"public"."deposit_status", 'confirming'::"public"."deposit_status", 'review'::"public"."deposit_status"]));


--
-- Name: ix_deposits_address; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_deposits_address" ON "public"."deposits" USING "btree" ("asset_id", "address", "created_at" DESC) WHERE ("address" IS NOT NULL);


--
-- Name: ix_deposits_monitoring; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_deposits_monitoring" ON "public"."deposits" USING "btree" ("monitoring_expires_at") WHERE ("status" = ANY (ARRAY['settled'::"public"."deposit_status", 'expired'::"public"."deposit_status", 'review'::"public"."deposit_status"]));


--
-- Name: ix_deposits_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_deposits_user" ON "public"."deposits" USING "btree" ("external_user_id", "created_at" DESC);


--
-- Name: ix_idem_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_idem_created" ON "public"."idempotency_keys" USING "btree" ("created_at");


--
-- Name: ix_outbound_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_outbound_due" ON "public"."outbound_events" USING "btree" ("next_attempt_at") WHERE ("status" = 'pending'::"text");


--
-- Name: ix_postings_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_postings_account" ON "public"."postings" USING "btree" ("account_id", "id" DESC);


--
-- Name: ix_postings_entry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_postings_entry" ON "public"."postings" USING "btree" ("entry_id");


--
-- Name: ix_wallet_txo_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_wallet_txo_open" ON "public"."wallet_txo_alerts" USING "btree" ("detected_at") WHERE ("status" = 'open'::"text");


--
-- Name: ix_webhook_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_webhook_pending" ON "public"."webhook_events" USING "btree" ("received_at") WHERE ("status" = 'received'::"text");


--
-- Name: ix_withdrawals_destination; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_withdrawals_destination" ON "public"."withdrawals" USING "btree" ("asset_id", "destination_address", "created_at" DESC);


--
-- Name: ix_withdrawals_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_withdrawals_pending" ON "public"."withdrawals" USING "btree" ("status") WHERE ("status" = ANY (ARRAY['pending_approval'::"public"."withdrawal_status", 'approved'::"public"."withdrawal_status", 'submitting'::"public"."withdrawal_status", 'submitted'::"public"."withdrawal_status", 'broadcast'::"public"."withdrawal_status"]));


--
-- Name: ix_withdrawals_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_withdrawals_user" ON "public"."withdrawals" USING "btree" ("external_user_id", "created_at" DESC);


--
-- Name: ix_withdrawals_velocity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_withdrawals_velocity" ON "public"."withdrawals" USING "btree" ("asset_id", "created_at" DESC);


--
-- Name: ux_accounts_system; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "ux_accounts_system" ON "public"."accounts" USING "btree" ("asset_id", "kind") WHERE ("external_user_id" IS NULL);


--
-- Name: ux_accounts_user; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "ux_accounts_user" ON "public"."accounts" USING "btree" ("asset_id", "kind", "external_user_id") WHERE ("external_user_id" IS NOT NULL);


--
-- Name: ux_withdrawals_txid; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "ux_withdrawals_txid" ON "public"."withdrawals" USING "btree" ("txid") WHERE ("txid" IS NOT NULL);


--
-- Name: postings trg_entry_balanced; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER "trg_entry_balanced" AFTER INSERT ON "public"."postings" DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "public"."assert_entry_balanced"();


--
-- Name: journal_entries trg_journal_entries_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "trg_journal_entries_immutable" BEFORE DELETE OR UPDATE ON "public"."journal_entries" FOR EACH ROW EXECUTE FUNCTION "public"."reject_ledger_mutation"();


--
-- Name: postings trg_postings_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "trg_postings_immutable" BEFORE DELETE OR UPDATE ON "public"."postings" FOR EACH ROW EXECUTE FUNCTION "public"."reject_ledger_mutation"();


--
-- Name: accounts accounts_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."accounts"
    ADD CONSTRAINT "accounts_asset_id_fkey" FOREIGN KEY ("asset_id") REFERENCES "public"."assets"("id");


--
-- Name: deposit_payments deposit_payments_deposit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."deposit_payments"
    ADD CONSTRAINT "deposit_payments_deposit_id_fkey" FOREIGN KEY ("deposit_id") REFERENCES "public"."deposits"("id");


--
-- Name: deposit_payments deposit_payments_ledger_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."deposit_payments"
    ADD CONSTRAINT "deposit_payments_ledger_entry_id_fkey" FOREIGN KEY ("ledger_entry_id") REFERENCES "public"."journal_entries"("id");


--
-- Name: deposits deposits_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."deposits"
    ADD CONSTRAINT "deposits_asset_id_fkey" FOREIGN KEY ("asset_id") REFERENCES "public"."assets"("id");


--
-- Name: journal_entries journal_entries_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."journal_entries"
    ADD CONSTRAINT "journal_entries_asset_id_fkey" FOREIGN KEY ("asset_id") REFERENCES "public"."assets"("id");


--
-- Name: journal_entries journal_entries_reverses_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."journal_entries"
    ADD CONSTRAINT "journal_entries_reverses_entry_id_fkey" FOREIGN KEY ("reverses_entry_id") REFERENCES "public"."journal_entries"("id");


--
-- Name: postings postings_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."postings"
    ADD CONSTRAINT "postings_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "public"."accounts"("id");


--
-- Name: postings postings_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."postings"
    ADD CONSTRAINT "postings_entry_id_fkey" FOREIGN KEY ("entry_id") REFERENCES "public"."journal_entries"("id");


--
-- Name: wallet_txo_alerts wallet_txo_alerts_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."wallet_txo_alerts"
    ADD CONSTRAINT "wallet_txo_alerts_asset_id_fkey" FOREIGN KEY ("asset_id") REFERENCES "public"."assets"("id");


--
-- Name: withdrawals withdrawals_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."withdrawals"
    ADD CONSTRAINT "withdrawals_asset_id_fkey" FOREIGN KEY ("asset_id") REFERENCES "public"."assets"("id");


--
-- Name: withdrawals withdrawals_hold_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."withdrawals"
    ADD CONSTRAINT "withdrawals_hold_entry_id_fkey" FOREIGN KEY ("hold_entry_id") REFERENCES "public"."journal_entries"("id");


--
-- Name: withdrawals withdrawals_release_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."withdrawals"
    ADD CONSTRAINT "withdrawals_release_entry_id_fkey" FOREIGN KEY ("release_entry_id") REFERENCES "public"."journal_entries"("id");


--
-- Name: withdrawals withdrawals_settle_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."withdrawals"
    ADD CONSTRAINT "withdrawals_settle_entry_id_fkey" FOREIGN KEY ("settle_entry_id") REFERENCES "public"."journal_entries"("id");


--
-- Name: withdrawals withdrawals_submit_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."withdrawals"
    ADD CONSTRAINT "withdrawals_submit_entry_id_fkey" FOREIGN KEY ("submit_entry_id") REFERENCES "public"."journal_entries"("id");


--
-- PostgreSQL database dump complete
--

\unrestrict OFGPPwKEIbCbIzKrlkCi5cdRprtdalnSLnwFZ9yDO9lXcZiBYD5UXHQRjKoGsV2

