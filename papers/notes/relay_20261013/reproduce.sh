#!/usr/bin/env bash
# Bounded public metadata lookup. Save to a new directory to preserve this run.
set -u
relay_output_dir="${1:?Usage: bash reproduce.sh OUTPUT_DIRECTORY}"
mkdir -p "$relay_output_dir"
crossref_fields='DOI,title,author,published,container-title,abstract,URL,link'
curl -sS --connect-timeout 10 --max-time 40 --get 'https://api.crossref.org/works' \
  --data-urlencode 'query.title=Transfer Optimization for Heterogeneous Drone Delivery and Pickup Problem' \
  --data-urlencode 'rows=3' --data-urlencode "select=$crossref_fields" \
  -o "$relay_output_dir/crossref_transfer_title.json" -w 'crossref_transfer_title HTTP %{http_code}\n'
curl -sS --connect-timeout 10 --max-time 40 --get 'https://api.crossref.org/works' \
  --data-urlencode 'query.title=An adaptive large neighborhood search for the pickup and delivery problem with transfers' \
  --data-urlencode 'rows=5' --data-urlencode "select=$crossref_fields" \
  -o "$relay_output_dir/crossref_pdptw_transfers.json" -w 'crossref_pdptw_transfers HTTP %{http_code}\n'
curl -sS --connect-timeout 10 --max-time 40 --get 'https://api.crossref.org/works' \
  --data-urlencode 'query.title=Efficient feasibility testing for pickup and delivery problems with transfers' \
  --data-urlencode 'rows=3' --data-urlencode "select=$crossref_fields" \
  -o "$relay_output_dir/crossref_transfer_feasibility.json" -w 'crossref_transfer_feasibility HTTP %{http_code}\n'
curl -sS --connect-timeout 10 --max-time 40 --get 'https://export.arxiv.org/api/query' \
  --data-urlencode 'search_query=(all:drone AND (all:transfer OR all:relay) AND all:delivery)' \
  --data-urlencode 'max_results=8' --data-urlencode 'start=0' --data-urlencode 'sortBy=relevance' \
  -o "$relay_output_dir/arxiv_drone_relay.xml" -w 'arxiv_drone_relay HTTP %{http_code}\n'
curl -sS -L --connect-timeout 10 --max-time 40 'https://arxiv.org/abs/2609.00065' \
  -o "$relay_output_dir/scientific_agent_skills_abs.html" -w 'citation_abs HTTP %{http_code}\n'
