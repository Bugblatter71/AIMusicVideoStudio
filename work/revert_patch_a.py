import os
import shutil
import sys

target_path = r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app\models\minimax_h3\components\video_autoencoder.py"
backup_path = os.path.abspath(r"work\video_autoencoder.py.bak")

if not os.path.exists(backup_path):
    print(f"Error: Backup file {backup_path} not found!")
    sys.exit(1)

shutil.copy2(backup_path, target_path)
print(f"Successfully restored video_autoencoder.py from {backup_path}!")

