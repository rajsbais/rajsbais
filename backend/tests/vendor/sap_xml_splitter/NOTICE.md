# SAP S/4HANA Migration Cockpit XML File Splitter (vendored for tests)

`splitter.py` is an unmodified copy of `src/splitter.py` from
https://github.com/SAP-samples/s4hana-mc-xml-file-splitter (Copyright 2025 SAP SE or an SAP affiliate company),
licensed under the Apache License 2.0 (see `LICENSE`). It is SAP's own tool for splitting real migration cockpit
XML files and is used here only as an executable check that the files the template-driven export produces have
the layout that tool expects (worksheet order, eight header rows, merged key cell in row 7, line-oriented XML).
It is not part of the platform's runtime and is not imported outside the test suite. Its `progressbar`
dependency is stubbed by the test.
