import queue
import threading
from abc import ABC
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Optional

from lightning_sdk.lightning_cloud.login import Auth
from lightning_sdk.utils.resolve import _resolve_teamspace
from lightning_utilities import StrEnum
from lightning_utilities.core.rank_zero import rank_zero_debug, rank_zero_only, rank_zero_warn

from litmodels import upload_model
from litmodels.integrations.imports import _LIGHTNING_AVAILABLE, _PYTORCHLIGHTNING_AVAILABLE
from litmodels.io.cloud import _list_available_teamspaces

if _LIGHTNING_AVAILABLE:
    from lightning.pytorch.callbacks import ModelCheckpoint as _LightningModelCheckpoint


if _PYTORCHLIGHTNING_AVAILABLE:
    from pytorch_lightning.callbacks import ModelCheckpoint as _PytorchLightningModelCheckpoint


if TYPE_CHECKING:
    if _LIGHTNING_AVAILABLE:
        import lightning.pytorch as pl
    if _PYTORCHLIGHTNING_AVAILABLE:
        import pytorch_lightning as pl


# Create a singleton upload manager
@lru_cache(maxsize=None)
def get_model_manager() -> "ModelManager":
    """Get or create the singleton upload manager."""
    return ModelManager()


# enumerate the possible actions
class Action(StrEnum):
    """Enumeration of possible actions for the ModelManager."""

    UPLOAD = "upload"
    REMOVE = "remove"


class ModelManager:
    """Manages uploads and removals with a single queue but separate counters."""

    task_queue: queue.Queue

    def __init__(self) -> None:
        """Initialize the ModelManager with a task queue and counters."""
        self.task_queue = queue.Queue()
        self.upload_count = 0
        self.remove_count = 0
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def __getstate__(self) -> dict:
        """Get the state of the ModelManager for pickling."""
        state = self.__dict__.copy()
        del state["task_queue"]
        del state["_worker"]
        return state

    def __setstate__(self, state: dict) -> None:
        """Set the state of the ModelManager after unpickling."""
        self.__dict__.update(state)
        import queue
        import threading

        self.task_queue = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            task = self.task_queue.get()
            if task is None:
                self.task_queue.task_done()
                break
            action, detail = task
            if action == Action.UPLOAD:
                registry_name, filepath = detail
                try:
                    upload_model(registry_name, filepath)
                    rank_zero_debug(f"Finished uploading: {filepath}")
                except Exception as ex:
                    rank_zero_warn(f"Upload failed {filepath}: {ex}")
                finally:
                    self.upload_count -= 1
            elif action == Action.REMOVE:
                trainer, filepath = detail
                try:
                    trainer.strategy.remove_checkpoint(filepath)
                    rank_zero_debug(f"Removed file: {filepath}")
                except Exception as ex:
                    rank_zero_warn(f"Removal failed {filepath}: {ex}")
                finally:
                    self.remove_count -= 1
            else:
                rank_zero_warn(f"Unknown task: {task}")
            self.task_queue.task_done()

    def queue_upload(self, registry_name: str, filepath: str) -> None:
        """Queue an upload task."""
        self.upload_count += 1
        self.task_queue.put((Action.UPLOAD, (registry_name, filepath)))
        rank_zero_debug(f"Queued upload: {filepath} (pending uploads: {self.upload_count})")

    def queue_remove(self, trainer: "pl.Trainer", filepath: str) -> None:
        """Queue a removal task."""
        self.remove_count += 1
        self.task_queue.put((Action.REMOVE, (trainer, filepath)))
        rank_zero_debug(f"Queued removal: {filepath} (pending removals: {self.remove_count})")

    def shutdown(self) -> None:
        """Shut down the manager and wait for all tasks to complete."""
        self.task_queue.put(None)
        self.task_queue.join()
        rank_zero_debug("Manager shut down.")


# Base class to be inherited
class LitModelCheckpointMixin(ABC):
    """Mixin class for LitModel checkpoint functionality."""

    _datetime_stamp: str
    model_registry: Optional[str] = None
    _model_manager: ModelManager

    def __init__(self, model_name: Optional[str]) -> None:
        """Initialize with model name."""
        if not model_name:
            rank_zero_warn(
                "The model is not defined so we will continue with LightningModule names and timestamp of now"
            )
        self._datetime_stamp = datetime.now().strftime("%Y%m%d-%H%M")
        # remove any / from beginning and end of the name
        self.model_registry = model_name.strip("/") if model_name else None

        try:  # authenticate before anything else starts
            Auth().authenticate()
        except Exception:
            raise ConnectionError("Unable to authenticate with Lightning Cloud. Check your credentials.")

        self._model_manager = ModelManager()

    @rank_zero_only
    def _upload_model(self, filepath: str) -> None:
        # todo: use filename as version but need to validate that such version does not exists yet
        if not self.model_registry:
            raise RuntimeError(
                "Model name is not specified neither updated by `setup` method via Trainer."
                " Please set the model name before uploading or ensure that `setup` method is called."
            )
        # Add to queue instead of uploading directly
        get_model_manager().queue_upload(self.model_registry, filepath)

    @rank_zero_only
    def _remove_model(self, trainer: "pl.Trainer", filepath: str) -> None:
        """Remove the local version of the model if requested."""
        get_model_manager().queue_remove(trainer, filepath)

    def default_model_name(self, pl_model: "pl.LightningModule") -> str:
        """Generate a default model name based on the class name and timestamp."""
        return pl_model.__class__.__name__ + f"_{self._datetime_stamp}"

    def _update_model_name(self, pl_model: "pl.LightningModule") -> None:
        """Update the model name if not already set."""
        count_slashes_in_name = self.model_registry.count("/") if self.model_registry else 0
        default_model_name = self.default_model_name(pl_model)
        if count_slashes_in_name > 2:
            raise ValueError(
                f"Invalid model name: '{self.model_registry}'. It should not contain more than two '/' character."
            )
        if count_slashes_in_name == 2:
            # user has defined the model name in the format 'organization/teamspace/modelname'
            return
        if count_slashes_in_name == 1:
            # user had defined only the teamspace name
            self.model_registry = f"{self.model_registry}/{default_model_name}"
        elif count_slashes_in_name == 0:
            if not self.model_registry:
                self.model_registry = default_model_name
            teamspace = _resolve_teamspace(None, None, None)
            if teamspace:
                # case you use default model name and teamspace determined from env. variables aka running in studio
                self.model_registry = f"{teamspace.owner.name}/{teamspace.name}/{self.model_registry}"
            else:  # try to load default users teamspace
                ts_names = list(_list_available_teamspaces().keys())
                if len(ts_names) == 1:
                    self.model_registry = f"{ts_names[0]}/{self.model_registry}"
                else:
                    options = "\n\t".join(ts_names)
                    raise RuntimeError(
                        f"Teamspace is not defined and there are multiple teamspaces available:\n{options}"
                    )
        else:
            raise RuntimeError(f"Invalid model name: '{self.model_registry}'")


# Create specific implementations
if _LIGHTNING_AVAILABLE:

    class LightningModelCheckpoint(LitModelCheckpointMixin, _LightningModelCheckpoint):
        """Lightning ModelCheckpoint with LitModel support.

        Args:
            model_name: Name of the model to upload in format 'organization/teamspace/modelname'
            args: Additional arguments to pass to the parent class.
            kwargs: Additional keyword arguments to pass to the parent class.
        """

        def __init__(self, *args: Any, model_name: Optional[str] = None, **kwargs: Any) -> None:
            """Initialize the checkpoint with model name and other parameters."""
            _LightningModelCheckpoint.__init__(self, *args, **kwargs)
            LitModelCheckpointMixin.__init__(self, model_name)

        def setup(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", stage: str) -> None:
            """Setup the checkpoint callback."""
            _LightningModelCheckpoint.setup(self, trainer, pl_module, stage)
            self._update_model_name(pl_module)

        def _save_checkpoint(self, trainer: "pl.Trainer", filepath: str) -> None:
            """Extend the save checkpoint method to upload the model."""
            _LightningModelCheckpoint._save_checkpoint(self, trainer, filepath)
            if trainer.is_global_zero:  # Only upload from the main process
                self._upload_model(filepath)

        def on_fit_end(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
            """Extend the on_fit_end method to ensure all uploads are completed."""
            _LightningModelCheckpoint.on_fit_end(self, trainer, pl_module)
            # Wait for all uploads to finish
            get_model_manager().shutdown()

        def _remove_checkpoint(self, trainer: "pl.Trainer", filepath: str) -> None:
            """Extend the remove checkpoint method to remove the model from the registry."""
            if trainer.is_global_zero:  # Only remove from the main process
                self._remove_model(trainer, filepath)


if _PYTORCHLIGHTNING_AVAILABLE:

    class PytorchLightningModelCheckpoint(LitModelCheckpointMixin, _PytorchLightningModelCheckpoint):
        """PyTorch Lightning ModelCheckpoint with LitModel support.

        Args:
            model_name: Name of the model to upload in format 'organization/teamspace/modelname'
            args: Additional arguments to pass to the parent class.
            kwargs: Additional keyword arguments to pass to the parent class.
        """

        def __init__(self, *args: Any, model_name: Optional[str] = None, **kwargs: Any) -> None:
            """Initialize the checkpoint with model name and other parameters."""
            _PytorchLightningModelCheckpoint.__init__(self, *args, **kwargs)
            LitModelCheckpointMixin.__init__(self, model_name)

        def setup(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", stage: str) -> None:
            """Setup the checkpoint callback."""
            _PytorchLightningModelCheckpoint.setup(self, trainer, pl_module, stage)
            self._update_model_name(pl_module)

        def _save_checkpoint(self, trainer: "pl.Trainer", filepath: str) -> None:
            """Extend the save checkpoint method to upload the model."""
            _PytorchLightningModelCheckpoint._save_checkpoint(self, trainer, filepath)
            if trainer.is_global_zero:  # Only upload from the main process
                self._upload_model(filepath)

        def on_fit_end(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
            """Extend the on_fit_end method to ensure all uploads are completed."""
            _PytorchLightningModelCheckpoint.on_fit_end(self, trainer, pl_module)
            # Wait for all uploads to finish
            get_model_manager().shutdown()

        def _remove_checkpoint(self, trainer: "pl.Trainer", filepath: str) -> None:
            """Extend the remove checkpoint method to remove the model from the registry."""
            if trainer.is_global_zero:  # Only remove from the main process
                self._remove_model(trainer, filepath)
