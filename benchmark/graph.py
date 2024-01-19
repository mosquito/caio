import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from scipy.interpolate import make_interp_spline


# Enable the dark theme
plt.style.use("dark_background")

# Load your CSV file
df = pd.read_csv(sys.argv[1])
df["time"] = df["time"] / 1e6  # Convert time to milliseconds

# Determine global Y-axis limits
global_y_min = df["time"].min()
global_y_max = df["time"].max()

def plot_and_save(data, x, y, title, file_name):
    fig, ax = plt.subplots(figsize=(10, 6))

    # Group by max_ops and plot each group
    for max_ops, group in data.groupby("max_ops"):
        # Averaging y values for each unique x value
        averaged_data = group.groupby(x, as_index=False)[y].mean()

        # Interpolating the data for smoothness
        x_vals = np.array(range(len(averaged_data[x])))
        y_vals = averaged_data[y].values
        spline = make_interp_spline(x_vals, y_vals)
        x_smooth = np.linspace(x_vals.min(), x_vals.max(), 600)
        y_smooth = spline(x_smooth)

        # Plotting the smooth line
        ax.plot(x_smooth, y_smooth, label=f"max_ops={max_ops}")

    ax.set_xticks(x_vals)
    ax.set_xticklabels(averaged_data[x].values, rotation=45)

    # Set the global Y-axis limits
    ax.set_ylim(global_y_min, global_y_max)

    ax.set_title(title)
    ax.set_xlabel(x)
    ax.set_ylabel(y + " (ms)")
    ax.grid(True)
    ax.legend()

    fig.savefig(file_name, format="svg")
    plt.close(fig)


def plot_time_distribution(data, group_vars, title, file_name):
    fig, ax = plt.subplots(figsize=(12, 8))
    sns.boxplot(x=group_vars[0], y="time", hue=group_vars[1], data=data, ax=ax)

    ax.set_ylabel("Time (ms)")
    ax.set_title(title)
    ax.grid(True)

    # Save the plot
    fig.savefig(file_name, format="svg")
    plt.close(fig)


# Group the data by implementation, operation, and max_ops for individual plots
grouped = df.groupby(["implementation", "operation"])

save_path = Path("graphs")
save_path.mkdir(exist_ok=True, parents=True)

for (implementation, operation), group_data in grouped:
    # 1. Execution Time vs Chunk Size
    plot_and_save(
        group_data, "chunk_size", "time",
        f"{implementation} - {operation} (Time vs Chunk Size)",
        save_path / f"{implementation}_{operation}_time_chunk_size.svg",
    )

    # 2. Execution Time vs Concurrency
    plot_and_save(
        group_data, "concurrency", "time",
        f"{implementation} - {operation} (Time vs Concurrency)",
        save_path / f"{implementation}_{operation}_time_concurrency.svg",
    )


plot_time_distribution(
    df, ['implementation', 'operation'],
    'Distribution of Execution Time across Implementations and Operations',
    save_path / 'execution_time_distribution.svg'
)

# Density plot
plt.figure(figsize=(12, 8))
sns.kdeplot(data=df, x='time', hue='implementation', fill=True, common_norm=False, palette='bright')
plt.title('Density Plot of Execution Time across Implementations')
plt.xlabel('Time (ms)')
plt.ylabel('Density')
plt.grid(True)
plt.savefig(save_path / 'density_plot.svg')
plt.close()
