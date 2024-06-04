import argparse
import copy

import torch
from torch.utils.tensorboard import SummaryWriter
from torch_geometric.nn import Node2Vec
from torch_geometric.seed import seed_everything
from torch_geometric.utils import to_undirected

from relbench.data import LinkTask, RelBenchDataset
from relbench.data.table import Table
from relbench.data.task_base import TaskType
from relbench.datasets import get_dataset

parser = argparse.ArgumentParser()
parser.add_argument("--dataset", type=str, default="rel-trial")
parser.add_argument("--task", type=str, default="condition-sponsor-rec")
parser.add_argument("--lr", type=float, default=0.01)
parser.add_argument("--epochs", type=int, default=3000)
parser.add_argument("--batch_size", type=int, default=512)
parser.add_argument("--embedding_dim", type=int, default=128)
parser.add_argument("--walk_length", type=int, default=4)
parser.add_argument("--context_size", type=int, default=2)
parser.add_argument("--num_workers", type=int, default=8)
parser.add_argument("--log_dir", type=str, default="results")
args = parser.parse_args()

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
seed_everything(42)

root_dir = "./data"

# TODO: remove process=True once correct data/task is uploaded.
dataset: RelBenchDataset = get_dataset(name=args.dataset, process=True)
task: LinkTask = dataset.get_task(args.task, process=True)
tune_metric = "link_prediction_map"
assert task.task_type == TaskType.LINK_PREDICTION

num_src_nodes = task.num_src_nodes
df = task.train_table.df.explode(task.dst_entity_col)

# Directional training table edges:
src = torch.from_numpy(df[task.src_entity_col].astype(int).values)
dst = torch.from_numpy(df[task.dst_entity_col].astype(int).values)

# Since both src nodes and dst nodes start from index 0, so if we
# use them directly, there will be overlaps in their indices. In order
# to construct edges, we need to add num_src_nodes to all dst nodes
# index. So src nodes will range from [0, num_src_nodes) and dst
# nodes will range from [num_src_nodes, num_dst_nodes + num_src_nodes).

edge_index = torch.stack([src, dst + num_src_nodes], dim=0)

model = Node2Vec(
    edge_index=to_undirected(edge_index),
    embedding_dim=args.embedding_dim,
    walk_length=args.walk_length,
    context_size=args.context_size,
    num_negative_samples=1,
    p=1.0,
    q=1.0,
    sparse=True,
).to(device)

loader = model.loader(
    batch_size=args.batch_size,
    shuffle=True,
    num_workers=args.num_workers,
)
optimizer = torch.optim.SparseAdam(model.parameters(), lr=args.lr)


def train():
    model.train()
    total_loss = 0
    for pos_rw, neg_rw in loader:
        optimizer.zero_grad()
        loss = model.loss(pos_rw.to(device), neg_rw.to(device))
        loss.backward()
        optimizer.step()
        total_loss += loss.item()
    return total_loss / len(loader)


def get_valid_test_dst_entities():
    dst_entity_table = task.dataset.db.table_dict[task.dst_entity_table]
    # Might be good to filter out entities based on time col in the future
    return dst_entity_table.df[task.dst_entity_col].values + num_src_nodes


@torch.no_grad()
def test(table: Table, k):
    model.eval()
    z = model()
    src = z[table.df[task.src_entity_col].values]
    dst = z[get_valid_test_dst_entities()]
    _, indices = torch.topk(src @ dst.T, k)
    dst_ids = df[task.dst_entity_col].astype(int).values
    mapped_tensor = torch.take(torch.from_numpy(dst_ids), indices)
    return mapped_tensor


writer = SummaryWriter(log_dir=args.log_dir)

state_dict = None
best_val_metric = 0

for epoch in range(1, args.epochs + 1):
    train_loss = train()
    val_pred = test(task.val_table, task.eval_k)
    val_metrics = task.evaluate(val_pred, task.val_table)
    print(
        f"Epoch: {epoch:02d}, Train loss: {train_loss}, " f"Val metrics: {val_metrics}"
    )

    if val_metrics[tune_metric] > best_val_metric:
        best_val_metric = val_metrics[tune_metric]
        state_dict = copy.deepcopy(model.state_dict())

    writer.add_scalar("train/loss", train_loss, epoch)
    for name, metric in val_metrics.items():
        writer.add_scalar(f"val/{name}", metric, epoch)

model.load_state_dict(state_dict)
val_pred = test(task.val_table, task.eval_k)
val_metrics = task.evaluate(val_pred, task.val_table)
print(f"Best Val metrics: {val_metrics}")

test_pred = test(task.test_table, task.eval_k)
test_metrics = task.evaluate(test_pred)
print(f"Best test metrics: {test_metrics}")

for name, metric in test_metrics.items():
    writer.add_scalar(f"test/{name}", metric, 0)

writer.flush()
writer.close()
