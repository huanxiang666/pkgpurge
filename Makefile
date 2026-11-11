PYTHON ?= python3

.PHONY: test mutation-check run selftest deb clean install

test:
	$(PYTHON) -m unittest discover -s tests -v

mutation-check:
	bash scripts/mutation-check.sh

run:
	$(PYTHON) -m pkgpurge

selftest:
	$(PYTHON) -m pkgpurge --selftest

deb:
	bash packaging/build-deb.sh

install:
	sudo bash packaging/install.sh /usr/local

clean:
	rm -rf build dist
	find . -name '__pycache__' -type d -exec rm -rf {} +
