@echo off
setlocal
python duty_audit.py || exit /b 1
python verify_evidence.py evidence\evidence.json || exit /b 1
python -m unittest discover -s tests -v || exit /b 1
echo All offline checks passed.
