CREATE TABLE schema_info (
    version INTEGER PRIMARY KEY
);

CREATE TABLE market_datasets (
    data_version VARCHAR PRIMARY KEY,
    manifest JSON NOT NULL,
    payload BLOB NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE instruments (
    instrument_id UUID PRIMARY KEY,
    venue VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    instrument_key VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    currency VARCHAR NOT NULL,
    exchange_timezone VARCHAR NOT NULL,
    source_ref VARCHAR NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (venue, instrument_key)
);

CREATE TABLE market_listings (
    data_version VARCHAR NOT NULL REFERENCES market_datasets(data_version),
    instrument_id UUID NOT NULL REFERENCES instruments(instrument_id),
    listed_on DATE NOT NULL,
    listing_available_at TIMESTAMPTZ NOT NULL,
    delisted_on DATE,
    delisting_available_at TIMESTAMPTZ,
    source_ref VARCHAR NOT NULL,
    PRIMARY KEY (data_version, instrument_id),
    CHECK ((delisted_on IS NULL AND delisting_available_at IS NULL)
        OR (delisted_on > listed_on AND delisting_available_at IS NOT NULL))
);

CREATE TABLE market_symbols (
    data_version VARCHAR NOT NULL,
    instrument_id UUID NOT NULL,
    effective_on DATE NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    symbol VARCHAR NOT NULL,
    source_ref VARCHAR NOT NULL,
    PRIMARY KEY (data_version, instrument_id, effective_on),
    FOREIGN KEY (data_version, instrument_id)
        REFERENCES market_listings(data_version, instrument_id)
);

CREATE TABLE market_pool_changes (
    data_version VARCHAR NOT NULL,
    pool_name VARCHAR NOT NULL,
    instrument_id UUID NOT NULL,
    effective_on DATE NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    is_member BOOLEAN NOT NULL,
    source_ref VARCHAR NOT NULL,
    PRIMARY KEY (data_version, pool_name, instrument_id, effective_on),
    FOREIGN KEY (data_version, instrument_id)
        REFERENCES market_listings(data_version, instrument_id)
);

CREATE TABLE market_session_status (
    data_version VARCHAR NOT NULL,
    instrument_id UUID NOT NULL,
    session_date DATE NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL CHECK (status IN ('halted', 'no_trade')),
    source_ref VARCHAR NOT NULL,
    PRIMARY KEY (data_version, instrument_id, session_date),
    FOREIGN KEY (data_version, instrument_id)
        REFERENCES market_listings(data_version, instrument_id)
);

CREATE TABLE market_adjusted_prices (
    data_version VARCHAR NOT NULL,
    instrument_id UUID NOT NULL,
    session_date DATE NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    adjusted_close DECIMAL(20,8) NOT NULL CHECK (adjusted_close > 0),
    method VARCHAR NOT NULL,
    source_ref VARCHAR NOT NULL,
    PRIMARY KEY (data_version, instrument_id, session_date, available_at),
    FOREIGN KEY (data_version, instrument_id)
        REFERENCES market_listings(data_version, instrument_id)
);

CREATE TABLE market_fundamentals (
    data_version VARCHAR NOT NULL,
    instrument_id UUID NOT NULL,
    metric VARCHAR NOT NULL,
    period_end DATE NOT NULL,
    disclosed_at TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    value DECIMAL(28,8) NOT NULL,
    unit VARCHAR NOT NULL,
    source_ref VARCHAR NOT NULL,
    PRIMARY KEY (data_version, instrument_id, metric, period_end, disclosed_at, available_at),
    FOREIGN KEY (data_version, instrument_id)
        REFERENCES market_listings(data_version, instrument_id),
    CHECK (available_at >= disclosed_at)
);

CREATE TABLE market_bars (
    data_version VARCHAR NOT NULL,
    instrument_id UUID NOT NULL,
    session_date DATE NOT NULL,
    bar_end_at TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    open DECIMAL(20,8) NOT NULL CHECK (open > 0),
    high DECIMAL(20,8) NOT NULL CHECK (high > 0),
    low DECIMAL(20,8) NOT NULL CHECK (low > 0),
    close DECIMAL(20,8) NOT NULL CHECK (close > 0),
    volume BIGINT NOT NULL CHECK (volume >= 0),
    tradable BOOLEAN NOT NULL,
    source_ref VARCHAR NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (data_version, instrument_id, session_date),
    FOREIGN KEY (data_version, instrument_id)
        REFERENCES market_listings(data_version, instrument_id),
    CHECK (available_at >= bar_end_at),
    CHECK (low <= least(open, close) AND high >= greatest(open, close)),
    CHECK (NOT tradable OR volume > 0)
);

CREATE TABLE strategies (
    strategy_id UUID PRIMARY KEY,
    name VARCHAR NOT NULL,
    code_version VARCHAR NOT NULL,
    config JSON NOT NULL,
    definition_hash VARCHAR NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE simulations (
    simulation_id UUID PRIMARY KEY,
    strategy_id UUID NOT NULL REFERENCES strategies(strategy_id),
    data_version VARCHAR NOT NULL REFERENCES market_datasets(data_version),
    data_manifest JSON NOT NULL,
    scenario_version VARCHAR NOT NULL,
    scenario_manifest JSON NOT NULL,
    start_session DATE NOT NULL,
    end_session DATE NOT NULL,
    currency VARCHAR NOT NULL,
    initial_cash DECIMAL(24,8) NOT NULL CHECK (initial_cash > 0),
    execution_config JSON NOT NULL,
    seed BIGINT NOT NULL,
    toolkit_revision VARCHAR NOT NULL,
    lock_hash VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (simulation_id, strategy_id),
    UNIQUE (simulation_id, data_version),
    CHECK (start_session <= end_session)
);

CREATE TABLE orders (
    order_id UUID PRIMARY KEY,
    simulation_id UUID NOT NULL,
    strategy_id UUID NOT NULL,
    instrument_id UUID NOT NULL REFERENCES instruments(instrument_id),
    origin VARCHAR NOT NULL CHECK (origin IN ('strategy', 'user')),
    side VARCHAR NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    submitted_tick BIGINT NOT NULL CHECK (submitted_tick > 0),
    submitted_at TIMESTAMPTZ NOT NULL,
    intent_seq INTEGER NOT NULL CHECK (intent_seq >= 0),
    target_tick BIGINT NOT NULL,
    FOREIGN KEY (simulation_id, strategy_id)
        REFERENCES simulations(simulation_id, strategy_id),
    UNIQUE (simulation_id, order_id),
    UNIQUE (simulation_id, order_id, instrument_id),
    UNIQUE (simulation_id, submitted_tick, intent_seq),
    CHECK (target_tick = submitted_tick + 1)
);

CREATE TABLE events (
    simulation_id UUID NOT NULL REFERENCES simulations(simulation_id),
    tick_seq BIGINT NOT NULL CHECK (tick_seq >= 0),
    event_seq INTEGER NOT NULL CHECK (event_seq >= 0),
    event_type VARCHAR NOT NULL,
    order_id UUID,
    instrument_id UUID REFERENCES instruments(instrument_id),
    effective_at TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    source_ref VARCHAR NOT NULL,
    payload JSON NOT NULL,
    PRIMARY KEY (simulation_id, tick_seq, event_seq),
    FOREIGN KEY (simulation_id, order_id) REFERENCES orders(simulation_id, order_id)
);

CREATE TABLE fills (
    fill_id UUID PRIMARY KEY,
    simulation_id UUID NOT NULL,
    order_id UUID NOT NULL UNIQUE,
    instrument_id UUID NOT NULL,
    data_version VARCHAR NOT NULL,
    session_date DATE NOT NULL,
    tick_seq BIGINT NOT NULL CHECK (tick_seq > 0),
    filled_at TIMESTAMPTZ NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    reference_price DECIMAL(20,8) NOT NULL CHECK (reference_price > 0),
    fill_price DECIMAL(20,8) NOT NULL CHECK (fill_price > 0),
    notional DECIMAL(24,8) NOT NULL CHECK (notional > 0),
    fee DECIMAL(24,8) NOT NULL CHECK (fee >= 0),
    FOREIGN KEY (simulation_id, order_id, instrument_id)
        REFERENCES orders(simulation_id, order_id, instrument_id),
    FOREIGN KEY (simulation_id, data_version)
        REFERENCES simulations(simulation_id, data_version),
    FOREIGN KEY (data_version, instrument_id, session_date)
        REFERENCES market_bars(data_version, instrument_id, session_date),
    UNIQUE (simulation_id, fill_id)
);

CREATE TABLE cash_ledger (
    simulation_id UUID NOT NULL REFERENCES simulations(simulation_id),
    tick_seq BIGINT NOT NULL CHECK (tick_seq >= 0),
    entry_seq INTEGER NOT NULL CHECK (entry_seq >= 0),
    fill_id UUID UNIQUE,
    entry_type VARCHAR NOT NULL CHECK (entry_type IN ('initial', 'trade')),
    amount DECIMAL(24,8) NOT NULL,
    booked_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (simulation_id, tick_seq, entry_seq),
    FOREIGN KEY (simulation_id, fill_id) REFERENCES fills(simulation_id, fill_id),
    CHECK (
        (entry_type = 'initial' AND tick_seq = 0 AND entry_seq = 0
            AND fill_id IS NULL AND amount > 0)
        OR (entry_type = 'trade' AND tick_seq > 0 AND fill_id IS NOT NULL)
    )
);

CREATE TABLE positions_daily (
    simulation_id UUID NOT NULL,
    session_date DATE NOT NULL,
    instrument_id UUID NOT NULL,
    tick_seq BIGINT NOT NULL CHECK (tick_seq > 0),
    data_version VARCHAR NOT NULL,
    mark_session DATE NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    mark_price DECIMAL(20,8) NOT NULL CHECK (mark_price > 0),
    market_value DECIMAL(24,8) NOT NULL CHECK (market_value >= 0),
    stale BOOLEAN NOT NULL,
    marked_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (simulation_id, session_date, instrument_id),
    FOREIGN KEY (simulation_id, data_version)
        REFERENCES simulations(simulation_id, data_version),
    FOREIGN KEY (data_version, instrument_id, mark_session)
        REFERENCES market_bars(data_version, instrument_id, session_date),
    CHECK (mark_session <= session_date),
    CHECK (stale = (mark_session < session_date))
);

CREATE TABLE experiments (
    experiment_id UUID PRIMARY KEY,
    strategy_id UUID NOT NULL REFERENCES strategies(strategy_id),
    name VARCHAR NOT NULL,
    hypothesis VARCHAR NOT NULL,
    config JSON NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (experiment_id, strategy_id)
);

CREATE TABLE backtest_runs (
    run_id UUID PRIMARY KEY,
    experiment_id UUID NOT NULL,
    strategy_id UUID NOT NULL,
    simulation_id UUID,
    mode VARCHAR NOT NULL CHECK (mode IN ('vectorized', 'event_driven')),
    status VARCHAR NOT NULL CHECK (status IN ('succeeded', 'failed')),
    data_version VARCHAR NOT NULL,
    data_manifest JSON NOT NULL,
    start_session DATE NOT NULL,
    end_session DATE NOT NULL,
    toolkit_revision VARCHAR NOT NULL,
    lock_hash VARCHAR NOT NULL,
    run_config JSON NOT NULL,
    metrics JSON,
    artifact_manifest JSON,
    error_code VARCHAR,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (experiment_id, strategy_id)
        REFERENCES experiments(experiment_id, strategy_id),
    FOREIGN KEY (simulation_id, strategy_id)
        REFERENCES simulations(simulation_id, strategy_id),
    FOREIGN KEY (simulation_id, data_version)
        REFERENCES simulations(simulation_id, data_version),
    CHECK (start_session <= end_session AND started_at <= finished_at),
    CHECK (mode <> 'event_driven' OR simulation_id IS NOT NULL),
    CHECK (mode <> 'vectorized' OR simulation_id IS NULL),
    CHECK ((status = 'succeeded' AND error_code IS NULL)
        OR (status = 'failed' AND error_code IS NOT NULL))
);
