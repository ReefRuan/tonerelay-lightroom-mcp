"""Unified CLI for fixed-WB scene segmentation, anchor sync, and export contact audit."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    _PKG_PARENT = str(Path(__file__).resolve().parents[1])
    if _PKG_PARENT not in sys.path:
        sys.path.insert(0, _PKG_PARENT)
    from scene_batch.batch_sync import build_sync_plan, execute_sync, read_anchors_from_lightroom
    from scene_batch.contact_audit import run_audit
    from scene_batch.lrops_client import LrOpsBridgeClient
    from scene_batch.scene_segment import resegment_from_json, run_segmentation
else:
    from .batch_sync import build_sync_plan, execute_sync, read_anchors_from_lightroom
    from .contact_audit import run_audit
    from .lrops_client import LrOpsBridgeClient
    from .scene_segment import resegment_from_json, run_segmentation


def _parse_kv_pairs(values: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError(f"Expected KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _parse_stems(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    parts = [p.strip().upper() for p in raw.replace("\n", ",").split(",") if p.strip()]
    return set(parts) or None


def _resolve_folder_id(client: LrOpsBridgeClient, folder_id: str | None, folder_name: str | None) -> str:
    if folder_id:
        return folder_id
    if not folder_name:
        raise ValueError("Provide --folder-id or --folder-name")
    folders = client.list_catalog_folders(name_contains=folder_name, depth=8, limit=200)
    exact = [f for f in folders if f.get("label") == folder_name]
    matches = exact or folders
    if not matches:
        raise ValueError(f"No Lightroom Catalog folder matching {folder_name!r}")
    if len(matches) > 1:
        labels = [f"{f.get('id')}: {'/'.join(f.get('path_labels', []))}" for f in matches[:5]]
        raise ValueError(f"Ambiguous folder name {folder_name!r}; pass --folder-id. Matches: {labels}")
    return matches[0]["id"]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="scene_batch", description="Zero-token Lightroom scene batch toolkit")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_seg = sub.add_parser("segment", help="Segment fixed-WB photos into lighting scenes (0 Lightroom calls)")
    p_seg.add_argument("--input", required=False, type=Path, default=None, help="Folder of RAW or JPEG photos")
    p_seg.add_argument("--resegment", type=Path, default=None, help="Re-segment from cached scenes.json (0 disk reads on photo folder)")
    p_seg.add_argument("--render-overview", action="store_true", help="Also render scene_overview.jpg during --resegment")
    p_seg.add_argument("--output", required=True, type=Path, help="Output directory for scenes.json and visual maps")
    p_seg.add_argument("--stems", default=None, help="Optional comma-separated stems filter")
    p_seg.add_argument("--window", type=int, default=5)
    p_seg.add_argument("--min-segment", type=int, default=5)
    p_seg.add_argument("--time-gap", type=float, default=180.0)
    p_seg.add_argument("--wb-jump", type=float, default=6.0)
    p_seg.add_argument("--cluster-threshold", type=float, default=6.0)
    p_seg.add_argument("--sensitivity", type=float, default=1.0)
    default_workers = min(4, os.cpu_count() or 1)
    workers_help = "Worker threads for local decoding (default: auto up to 4; pass 1 for sequential)"

    p_seg.add_argument("--workers", type=int, default=default_workers, help=workers_help)

    p_anc = sub.add_parser("read-anchors", help="Read calibrated anchor virtual copies from Lightroom into scene_recipes.json")
    p_anc.add_argument("--scenes", required=True, type=Path)
    p_anc.add_argument("--output", required=True, type=Path)
    p_anc.add_argument("--anchor", action="append", help="Mapping S01=<vc_uuid> (repeatable)")
    p_anc.add_argument("--folder-id", default=None)
    p_anc.add_argument("--folder-name", default=None)
    p_anc.add_argument("--include-crop", action="store_true")

    p_plan = sub.add_parser("plan", help="Build sync_plan.json from scenes.json + scene_recipes.json + Catalog")
    p_plan.add_argument("--scenes", required=True, type=Path)
    p_plan.add_argument("--recipes", required=True, type=Path)
    p_plan.add_argument("--output", required=True, type=Path)
    p_plan.add_argument("--folder-id", default=None)
    p_plan.add_argument("--folder-name", default=None)
    p_plan.add_argument("--flag", choices=["pick", "unflagged", "reject", "all"], default="pick")
    p_plan.add_argument("--stems", default=None)
    p_plan.add_argument("--auto-exposure", action="store_true")
    p_plan.add_argument("--max-exp-delta", type=float, default=0.35)
    p_plan.add_argument("--auto-nr", action="store_true")
    p_plan.add_argument("--overrides", type=Path, default=None)
    p_plan.add_argument("--create-if-protected", action="store_true")

    p_sync = sub.add_parser("sync", help="Execute 0-token batch apply and serial export against lr-ops bridge")
    p_sync.add_argument("--plan", required=True, type=Path)
    p_sync.add_argument("--state", required=True, type=Path)
    p_sync.add_argument("--output-dir", type=Path, default=None)
    p_sync.add_argument("--batch-size", type=int, default=12)
    p_sync.add_argument("--apply-only", action="store_true")
    p_sync.add_argument("--export-only", action="store_true")
    p_sync.add_argument("--stems", default=None)
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.add_argument("--max-edge", type=int, default=0)
    p_sync.add_argument("--reencode-quality", type=int, default=None)
    p_sync.add_argument("--max-file-kb", type=int, default=None, help="Target max JPEG file size in KB (e.g. 2000 for ~2 MB)")
    p_sync.add_argument("--min-quality", type=int, default=68, help="Minimum preferred JPEG quality before smooth resize")
    p_sync.add_argument("--keep-resolution", action="store_true", help="Do not downscale when enforcing --max-file-kb")
    p_sync.add_argument("--force-reapply", action="store_true")
    p_sync.add_argument("--force-reexport", action="store_true")

    p_aud = sub.add_parser("audit", help="Analyze exported JPEGs, flag outliers, and render contact sheets")
    p_aud.add_argument("--input", required=True, type=Path)
    p_aud.add_argument("--output", required=True, type=Path)
    p_aud.add_argument("--scenes", type=Path, default=None)
    p_aud.add_argument("--cols", type=int, default=5)
    p_aud.add_argument("--rows", type=int, default=4)
    p_aud.add_argument("--cell-width", type=int, default=420)
    p_aud.add_argument("--wb-outlier-delta", type=float, default=6.5)
    p_aud.add_argument("--exp-l-delta", type=float, default=16.0)
    p_aud.add_argument("--clip-high", type=float, default=7.5)
    p_aud.add_argument("--grid", action="store_true", help="Draw horizon/rule-of-thirds and coordinate ticks for crop & straighten inspection")
    p_aud.add_argument("--workers", type=int, default=default_workers, help=workers_help)

    for p_bridge in (p_anc, p_plan, p_sync):
        p_bridge.add_argument(
            "--config",
            type=Path,
            default=None,
            help="Optional path to ToneRelay workbench config.json (auto-discovered if omitted)",
        )

    args = parser.parse_args(argv)

    if args.cmd == "segment":
        if args.resegment is not None:
            res = resegment_from_json(
                args.resegment,
                args.output,
                window=args.window,
                min_segment=args.min_segment,
                time_gap_sec=args.time_gap,
                wb_jump_threshold=args.wb_jump,
                cluster_threshold=args.cluster_threshold,
                sensitivity=args.sensitivity,
                render_images=args.render_overview,
            )
        elif args.input is not None:
            res = run_segmentation(
                args.input,
                args.output,
                stems_filter=_parse_stems(args.stems),
                window=args.window,
                min_segment=args.min_segment,
                time_gap_sec=args.time_gap,
                wb_jump_threshold=args.wb_jump,
                cluster_threshold=args.cluster_threshold,
                sensitivity=args.sensitivity,
                workers=args.workers,
            )
        else:
            parser.error("segment requires either --input or --resegment")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "read-anchors":
        client = LrOpsBridgeClient(config_path=args.config)
        client.probe()
        scenes_data = json.loads(args.scenes.read_text(encoding="utf-8"))
        fid = _resolve_folder_id(client, args.folder_id, args.folder_name) if (args.folder_id or args.folder_name) else None
        recipes = read_anchors_from_lightroom(
            client, scenes_data, _parse_kv_pairs(args.anchor), folder_id=fid, include_crop=args.include_crop
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(recipes, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "ok": True,
            "recipes_path": str(args.output),
            "scene_count": len(recipes["scenes"]),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "plan":
        client = LrOpsBridgeClient(config_path=args.config)
        client.probe()
        fid = _resolve_folder_id(client, args.folder_id, args.folder_name)
        scenes_data = json.loads(args.scenes.read_text(encoding="utf-8"))
        recipes_data = json.loads(args.recipes.read_text(encoding="utf-8"))
        overrides = json.loads(args.overrides.read_text(encoding="utf-8")) if args.overrides else None
        catalog_photos = client.list_folder_photos([fid], include_virtual_copies=True)
        plan = build_sync_plan(
            scenes_data,
            recipes_data,
            catalog_photos,
            flag_filter=args.flag,
            stems_filter=_parse_stems(args.stems),
            auto_exposure=args.auto_exposure,
            max_exp_delta=args.max_exp_delta,
            auto_nr=args.auto_nr,
            overrides=overrides,
            create_if_protected=args.create_if_protected,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "ok": True,
            "plan_path": str(args.output),
            "item_count": plan["item_count"],
            "skipped_count": plan["skipped_count"],
            "needs_vc_count": sum(1 for x in plan["items"] if x["needs_vc"]),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "sync":
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        client = None if args.dry_run else LrOpsBridgeClient(config_path=args.config)
        res = execute_sync(
            client,
            plan,
            args.state,
            output_dir=args.output_dir,
            batch_size=args.batch_size,
            apply_only=args.apply_only,
            export_only=args.export_only,
            stems_filter=_parse_stems(args.stems),
            dry_run=args.dry_run,
            max_edge=args.max_edge,
            reencode_quality=args.reencode_quality,
            max_file_kb=args.max_file_kb,
            min_quality=args.min_quality,
            keep_resolution=args.keep_resolution,
            force_reapply=args.force_reapply,
            force_reexport=args.force_reexport,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "audit":
        res = run_audit(
            args.input,
            args.output,
            scenes_path=args.scenes,
            cols=args.cols,
            rows=args.rows,
            cell_width=args.cell_width,
            wb_outlier_delta=args.wb_outlier_delta,
            exp_l_delta=args.exp_l_delta,
            clip_high_threshold=args.clip_high,
            workers=args.workers,
            grid=args.grid,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
