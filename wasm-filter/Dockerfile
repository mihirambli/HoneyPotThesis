FROM rust:latest

RUN rustup target add wasm32-unknown-unknown

WORKDIR /build
COPY Cargo.toml ./
COPY src/ ./src/

RUN cargo build --target wasm32-unknown-unknown --release

CMD ["cp", "/build/target/wasm32-unknown-unknown/release/wasm_filter.wasm", "/output/filter.wasm"]
