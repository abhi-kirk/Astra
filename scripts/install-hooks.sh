#!/bin/sh
# Run once after cloning to install the git pre-commit hook.
# The hook runs pytest before every commit — fails the commit if tests fail.

cat > .git/hooks/pre-commit << 'EOF'
#!/bin/sh
source .venv/bin/activate
pytest
EOF

chmod +x .git/hooks/pre-commit
echo "pre-commit hook installed — pytest will run on every commit"
