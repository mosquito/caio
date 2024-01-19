import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
from scipy.interpolate import make_interp_spline

# Enable the dark theme
plt.style.use('dark_background')

# Load your CSV file
df = pd.read_csv(sys.argv[1])
df['time'] = df['time'] / 1e6  # Convert time to milliseconds

def plot_and_save(data, x, y, title, file_name):
    fig, ax = plt.subplots(figsize=(10, 6))

    # Group by max_ops and plot each group
    for max_ops, group in data.groupby('max_ops'):
        # Averaging y values for each unique x value
        averaged_data = group.groupby(x, as_index=False)[y].mean()

        # Interpolating the data for smoothness
        x_vals = np.array(range(len(averaged_data[x])))
        y_vals = averaged_data[y].values
        spline = make_interp_spline(x_vals, y_vals)
        x_smooth = np.linspace(x_vals.min(), x_vals.max(), 600)
        y_smooth = spline(x_smooth)

        # Plotting the smooth line
        ax.plot(x_smooth, y_smooth, label=f'max_ops={max_ops}')

    ax.set_xticks(x_vals)
    ax.set_xticklabels(averaged_data[x].values, rotation=45)

    ax.set_title(title)
    ax.set_xlabel(x)
    ax.set_ylabel(y + ' (ms)')
    ax.grid(True)
    ax.legend()

    fig.savefig(file_name, format='svg')
    plt.close(fig)

# Group the data by implementation, operation and max_ops for individual plots
grouped = df.groupby(['implementation', 'operation'])

save_path = Path('graphs')
save_path.mkdir(exist_ok=True, parents=True)

for (implementation, operation), group_data in grouped:
    # 1. Execution Time vs Chunk Size
    plot_and_save(
        group_data, 'chunk_size', 'time',
        f'{implementation} - {operation} (Time vs Chunk Size)',
        save_path / f'{implementation}_{operation}_time_chunk_size.svg'
    )

    # 2. Execution Time vs Concurrency
    plot_and_save(
        group_data, 'concurrency', 'time',
        f'{implementation} - {operation} (Time vs Concurrency)',
        save_path / f'{implementation}_{operation}_time_concurrency.svg'
    )
