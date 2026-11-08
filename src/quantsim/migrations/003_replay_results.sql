CREATE TABLE replay_results (
    simulation_id UUID NOT NULL REFERENCES simulations(simulation_id),
    tick_seq BIGINT NOT NULL CHECK (tick_seq > 0),
    benchmark_instruments VARCHAR NOT NULL CHECK (json_valid(benchmark_instruments)),
    metrics JSON NOT NULL,
    report_path VARCHAR,
    report_sha256 VARCHAR,
    PRIMARY KEY (simulation_id, tick_seq, benchmark_instruments),
    CHECK ((report_path IS NULL) = (report_sha256 IS NULL))
);
