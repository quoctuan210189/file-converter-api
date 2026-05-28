#!/usr/bin/env bash
set -e

# Cài Python packages
pip install -r requirements.txt

# Cài LibreOffice và Ghostscript
apt-get update -qq
apt-get install -y -qq libreoffice ghostscript
