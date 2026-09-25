"""Entry point for the packaged app.

Double-clicking opens the window. The app also re-runs itself for background work:
  --worker <run folder>                     supervises one build (survives closing the window)
  --builder <args...>                       the build itself (script + voiceover -> Premiere XML)
  --finish-update <zip> <dir> <old pid>      applies a downloaded update, then relaunches
"""
import multiprocessing
import sys


def main():
    args = sys.argv[1:]
    if args[:1] == ["--worker"]:
        import job_worker
        job_worker.run(args[1])
    elif args[:1] == ["--builder"]:
        sys.argv = [sys.argv[0]] + args[1:]
        import skeleton_builder
        skeleton_builder.main()
    elif args[:1] == ["--finish-update"]:
        import updater
        updater.finish_update(*args[1:4])
    else:
        import app
        app.main(smoke_test="--smoke-test" in args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
