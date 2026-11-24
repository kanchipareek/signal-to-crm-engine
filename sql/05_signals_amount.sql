-- Migration: amount raised, for money_raised signals (SEC Form D).
-- Run ONCE against an existing database (re-running errors - the column
-- already exists). Fresh databases get this column from 01_signals.sql.
ALTER TABLE signals ADD COLUMN amount_usd INTEGER;
