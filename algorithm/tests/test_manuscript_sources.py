"""Portable figure provenance must retain exact source and byte validation."""

from copy import deepcopy

import pytest

from writing import build_final_manuscript as manuscript


@pytest.fixture
def relocated_figures(monkeypatch, tmp_path):
    root = tmp_path / "relocated-checkout"
    benchmark = root / "algorithm/results/final_benchmarks"
    scenario = root / "algorithm/results/final_scenario_comparison"
    sensitivity = root / "algorithm/results/final_sensitivity"
    figures = root / "writing/figures/final"
    monkeypatch.setattr(manuscript, "ROOT", root)
    inputs = {
        "benchmark_results": benchmark / "results.json",
        "scenario_runs": scenario / "runs.json",
        "scenario_manifest": scenario / "manifest.json",
        "sensitivity_runs": sensitivity / "runs.json",
        "sensitivity_manifest": sensitivity / "manifest.json",
        "sensitivity_summary": sensitivity / "summary.json",
        "sensitivity_stability": sensitivity / "stability.json",
        **{f"route_solution_{name}": scenario / "run_solutions" / f"{name}__seed_2026081701.json"
           for name in manuscript.SCENARIO_IDS},
    }
    sources = {"renderer": root / "algorithm/experiments/render_final_figures.py",
               "plot_style": root / "algorithm/experiments/final_plot_style.py"}
    outputs = [figures / filename for filename in manuscript.FIGURE_FILES]
    outputs += [path.with_suffix(".svg") for path in outputs]
    for path in [*inputs.values(), *outputs, *sources.values()]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())

    def record(path, base):
        relative = path.relative_to(base).as_posix()
        return {"relative_path": relative, "absolute_path": "/old-host/checkout/" + relative,
                "sha256": manuscript._sha256(path)}

    manifest = {"schema_version": 1, "repository_root": "/old-host/checkout", "route_seed": 2026081701,
                "figure_count": 9, "output_file_count": 18,
                "inputs": {key: record(path, root) for key, path in inputs.items()},
                "sources": {key: {"relative_path": path.relative_to(root).as_posix(),
                                  "sha256": manuscript._sha256(path)} for key, path in sources.items()},
                "outputs": [record(path, figures) for path in outputs]}
    monkeypatch.setattr(manuscript, "_load_json", lambda *_args: deepcopy(manifest))

    def validate():
        manuscript._validate_figure_manifest(figures, benchmark_dir=benchmark,
                                             scenario_dir=scenario, sensitivity_dir=sensitivity)
    return validate, manifest, inputs, outputs


def test_figure_manifest_accepts_relocated_checkout_with_original_absolute_paths(relocated_figures):
    validate, _, _, _ = relocated_figures
    validate()


@pytest.mark.parametrize("path", ["../results.json", "/other/results.json", "algorithm/results/../results.json"])
def test_figure_manifest_rejects_input_traversal_or_absolute_paths(relocated_figures, path):
    validate, manifest, _, _ = relocated_figures
    manifest["inputs"]["benchmark_results"]["relative_path"] = path
    with pytest.raises(manuscript.ArtefactError, match="规范相对路径"):
        validate()


def test_figure_manifest_rejects_wrong_source_even_with_matching_bytes(relocated_figures):
    validate, manifest, inputs, _ = relocated_figures
    inputs["scenario_manifest"].write_bytes(inputs["scenario_runs"].read_bytes())
    manifest["inputs"]["scenario_runs"]["relative_path"] = manifest["inputs"]["scenario_manifest"]["relative_path"]
    with pytest.raises(manuscript.ArtefactError, match="路径与正式结果目录不一致"):
        validate()


@pytest.mark.parametrize("prefix", ["../", "other/"])
def test_figure_manifest_rejects_output_path_with_expected_basename(relocated_figures, prefix):
    validate, manifest, _, _ = relocated_figures
    manifest["outputs"][0]["relative_path"] = prefix + manifest["outputs"][0]["relative_path"]
    with pytest.raises(manuscript.ArtefactError, match="相对路径|不是预期图像"):
        validate()


def test_figure_manifest_rejects_changed_file_in_relocated_checkout(relocated_figures):
    validate, _, _, outputs = relocated_figures
    outputs[0].write_bytes(b"changed figure")
    with pytest.raises(manuscript.ArtefactError, match="SHA-256不匹配"):
        validate()


def test_figure_manifest_rejects_source_symlink_outside_checkout(relocated_figures, tmp_path):
    validate, _, inputs, _ = relocated_figures
    outside = tmp_path / "outside.json"
    outside.write_bytes(inputs["benchmark_results"].read_bytes())
    inputs["benchmark_results"].unlink()
    inputs["benchmark_results"].symlink_to(outside)
    with pytest.raises(manuscript.ArtefactError, match="符号链接越出"):
        validate()


def test_figure_manifest_rejects_stale_rendering_source(relocated_figures):
    validate, _, _, _ = relocated_figures
    (manuscript.ROOT / "algorithm/experiments/render_final_figures.py").write_text("changed renderer")
    with pytest.raises(manuscript.ArtefactError, match="源码renderer的SHA-256已过期"):
        validate()


@pytest.mark.parametrize("change", ["missing", "unexpected"])
def test_figure_manifest_requires_exact_rendering_source_set(relocated_figures, change):
    validate, manifest, _, _ = relocated_figures
    if change == "missing":
        del manifest["sources"]["plot_style"]
    else:
        manifest["sources"]["other"] = deepcopy(manifest["sources"]["renderer"])
    with pytest.raises(manuscript.ArtefactError, match="恰含renderer与plot_style"):
        validate()


def test_figure_manifest_rejects_wrong_rendering_source_path(relocated_figures):
    validate, manifest, _, _ = relocated_figures
    manifest["sources"]["renderer"] = deepcopy(manifest["sources"]["plot_style"])
    with pytest.raises(manuscript.ArtefactError, match="路径与指定绘图源码不一致"):
        validate()


@pytest.fixture
def relocated_input(monkeypatch, tmp_path):
    root = tmp_path / "another-checkout"
    path = root / manuscript.OFFICIAL_INPUT_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text("known official data", encoding="utf-8")
    monkeypatch.setattr(manuscript, "ROOT", root)
    return {"input": "/original-host/repo/" + manuscript.OFFICIAL_INPUT_RELATIVE.as_posix(),
            "input_sha256": manuscript._sha256(path)}


def test_official_input_can_move_with_checkout(relocated_input):
    manuscript._verify_manifest_input(relocated_input, "test")


def test_official_input_relocation_keeps_strict_hash_check(relocated_input):
    relocated_input["input_sha256"] = "0" * 64
    with pytest.raises(manuscript.ArtefactError, match="输入SHA-256"):
        manuscript._verify_manifest_input(relocated_input, "test")


@pytest.mark.parametrize("path", ["/other/source.csv", "../algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv"])
def test_official_input_rejects_unexpected_source_or_traversal(relocated_input, path):
    relocated_input["input"] = path
    with pytest.raises(manuscript.ArtefactError, match="不是正式仓库CSV来源"):
        manuscript._verify_manifest_input(relocated_input, "test")
