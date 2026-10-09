# Builds every data figure and the generated LaTeX snippets from paper/data (make -C paper/figures).
PY ?= python3
FIGS = fig_m1_return_geometry fig_m2_wind_feasibility fig_m3_perception_gap fig_overall_heatmap fig_overall_outcomes \
       fig_ablation fig_front fig_coverage_eval fig_delegation fig_sensitivity fig_micro fig_stream

.PHONY: all figures numbers paper clean
all: figures numbers

figures:
	@for f in $(FIGS); do echo "$$f"; $(PY) $$f.py || exit 1; done

numbers:
	$(PY) gen_numbers.py

paper: all
	cd .. && tectonic -X compile main.tex

clean:
	rm -f pdf/*.pdf pdf/*.png
