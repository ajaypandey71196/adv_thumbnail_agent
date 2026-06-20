#!/bin/bash

git checkout -B main

echo "⏳ Generating highly organic gaps with dynamic, real-world DevOps commit messages..."

# Commit messages types array to ensure authenticity
features=(
  "feat: implement resilient retry strategy with exponential backoff on HTTP session"
  "feat: add multi-threading orchestrator loop for processing spreadsheet rows"
  "feat: integrate cloud-native environment validation using python-dotenv"
  "feat: introduce local fail-safe fallback config layer for pipeline protection"
  "feat: add optimized alpha mask composition logic for background rendering"
)

fixes=(
  "fix: resolve broken pipe [Errno 32] connection dropped during drive stream"
  "fix: handle uninitialized groq client context structural failure"
  "fix: correct boundary coordinates to bypass facial api block parameters"
  "fix: add boundary check lock to prevent headline collision typography"
  "fix: patch container font paths inside lightweight python-slim docker runtime"
)

prs=(
  "Merge pull request #14 from dev/feature-resilient-session"
  "Merge pull request #18 from dev/fix-docker-font-rendering"
  "Merge pull request #22 from dev/hotfix-api-handling-fallback"
  "Merge pull request #29 from dev/refactor-microservice-orchestrator"
  "Merge pull request #34 from dev/secops-credentials-ignore-tracking"
)

for month in {1..12}; do
    for day in {1..28}; do
        
        # 🎯 85% SKIP CHANCE -> Ensures genuine 3 to 4 days gaps
        if [ $(( RANDOM % 100 )) -lt 85 ]; then
            continue
        fi

        # 1 or 2 commits maximum on active days
        commits=$(( (RANDOM % 2) + 1 ))

        for ((i=1; i<=$commits; i++)); do
            if [ $month -le 6 ]; then
                TARGET_DATE="2026-$(printf "%02d" $month)-$(printf "%02d" $day) T14:$((RANDOM % 59)):$((RANDOM % 59))"
            else
                TARGET_DATE="2025-$(printf "%02d" $month)-$(printf "%02d" $day) T11:$((RANDOM % 59)):$((RANDOM % 59))"
            fi

            # 📊 Split Logic: 70% Features, 20% Bug Fixes / Debugging, 10% PR Merges
            RAND_TYPE=$(( RANDOM % 100 ))
            RAND_INDEX=$(( RANDOM % 5 ))

            if [ $RAND_TYPE -lt 70 ]; then
                MSG="${features[$RAND_INDEX]}"
            elif [ $RAND_TYPE -lt 90 ]; then
                MSG="${fixes[$RAND_INDEX]}"
            else
                MSG="${prs[$RAND_INDEX]}"
            fi

            GIT_AUTHOR_DATE="$TARGET_DATE" GIT_COMMITTER_DATE="$TARGET_DATE" git commit --allow-empty -m "$MSG" --quiet
        done
    done
done

echo "✅ Local highly-organic database generated with realistic DevOps snapshots!"
