PYTHON ?= python3

.PHONY: test run selftest deb clean install

test:
	$(PYTHON) -m unittest discover -s tests -v

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
