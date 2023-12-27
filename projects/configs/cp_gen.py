conti=True
# conti=False
repeat=10

num=10

import os
path="/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/projects/configs/toexe"
exe_list=os.listdir("/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/projects/configs/toexe")
exe_list = sorted(exe_list)


gpu=" 1"
print("\n"*30)
if exe_list[0][0]=='.':
    exe_list=exe_list[1:]
for i in exe_list:
    i=i
    with open("/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/projects/configs/toexe/"+i, 'r') as f:
        lines = f.readlines()
        lines[0] = 'work_dir = \'result/'+i[:-3]+'\'\n'
    with open("/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/projects/configs/toexe/"+i, 'w') as f:
        f.writelines(lines)
    for j in range(2,num+1):
        with open("/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/projects/configs/toexe/"+i[:-3]+'_'+str(j)+'.py', 'w') as f:
            lines[0] = 'work_dir = \'result/'+i[:-3]+'_'+str(j)+'\'\n'
            f.writelines(lines)



