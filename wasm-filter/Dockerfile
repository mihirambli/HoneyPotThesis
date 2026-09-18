FROM rust:latest

RUN rustup target add wasm32-unknown-unknown

WORKDIR /build
# Cargo.lock pins the crate versions the benchmark results were produced with.
COPY Cargo.toml Cargo.lock ./
COPY src/ ./src/

RUN cargo build --locked --target wasm32-unknown-unknown --release

CMD ["cp", "/build/target/wasm32-unknown-unknown/release/wasm_filter.wasm", "/output/filter.wasm"]
