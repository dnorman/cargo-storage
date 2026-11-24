.PHONY: build test integration

build: .build/cargo-storage

.build/cargo-storage: Sources/CargoStorage.swift
	mkdir -p .build
	xcrun swiftc -O -target $$(uname -m)-apple-macosx14.0 -module-cache-path .build/swift-cache $< -o $@

test: build
	python3 -m unittest discover -s tests -p 'test_*.py' -v

integration: test
	python3 tests/integration.py --binary .build/cargo-storage
