@echo off
set USERPROFILE=%USERPROFILE%\.claude-personal
claude --model claude-opus-4-8 --dangerously-skip-permissions
