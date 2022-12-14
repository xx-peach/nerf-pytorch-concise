import numpy as np
import os, imageio


def load_llff_data(basedir, factor=8, recenter=True, bd_factor=.75, spherify=False, llffhold=8, split='train', no_ndc=True, path_zflat=False):
    """ Loading llff data, eg. fern, ...
    Args:
        basedir   - the input data directory
        factor    - the scale of the image that we needed
        recenter  - whether to transfer the world coordinate to average camera coordinate
        bd_factor - the scale of the z values in world coordinates
        spherify  - whether to generate spherify poses
        llffhold  - will take every 1/N images as LLFF test set, paper uses 8
        no_ndc    - do not use normalized device coordinates (set for non-forward facing scenes)
        split     - ['train', 'val', 'test', 'fake']
    Returns:
        imgs  - shape of (N, H, W, 3), all the loaded images
        poses - shape of (N, 4, 4), all the loaded poses
        near  - shape of (1, ), z value of near plane
        far   - shape of (1, ), z value of the far plane
        H     - height of the image
        W     - width of the image
        K     - camera's intrinsic matrix
    """
    # load the original camera poses(c2w poses + intrinsic), near + far boundaries, and imagess
    poses, bds, imgs = _load_data(basedir, factor=factor)   # factor=8 downsamples original imgs by 8x
    # poses[0:3, 0:4] camera2world matrix, poses[0:3, 5] camera intrinsic, H, W, focal
    print('loaded from {}, minimum boundary is {}, maximum boundary is {}'.format(basedir, bds.min(), bds.max()))
    
    # correct rotation matrix ordering and move variable dim to axis 0
    poses = np.concatenate([poses[:, 1:2, :], -poses[:, 0:1, :], poses[:, 2:, :]], 1)   # (3, 5, N)
    poses = np.moveaxis(poses, -1, 0).astype(np.float32)    # (N, 3, 5)
    imgs  = np.moveaxis(imgs,  -1, 0).astype(np.float32)    # (N, H, W, 3)
    bds   = np.moveaxis(bds,   -1, 0).astype(np.float32)    # (N, 2), bds 里面存的每张 images 的 near, far z_vals 是在 camera 坐标系下的
    
    # rescale if bd_factor is provided, 这边缩放的不是图片尺寸，而是整个相机和世界坐标系下的坐标尺度
    sc = 1. if bd_factor is None else 1./(bds.min() * bd_factor)
    poses[:, :3, 3] *= sc   # 缩放相机在世界坐标系下的坐标
    bds *= sc               # 缩放 near 和 far 的深度值
    
    # re-center all the cameras，将所有相机的 poses 都转化到中心相机坐标系下，即世界坐标系变成了中心相机坐标系
    if recenter:
        poses = recenter_poses(poses)

    # 如果是球面坐标的话，就转化原本的 poses 并生成 spherify 的 render poses，非正面数据用这个
    if spherify:
        poses, render_poses, bds = spherify_poses(poses, bds)
    # 如果是正面拍摄的数据的话，就用下面这个生成 spiral render poses
    else:
        # get average pose
        c2w = poses_avg(poses)
        up = normalize(poses[:, :3, 1].sum(0))
        # find a reasonable "focus depth" for this dataset
        close_depth, inf_depth = bds.min() * .9, bds.max() * 5.
        dt = .75
        mean_dz = 1./(((1.-dt)/close_depth + dt/inf_depth))
        focal = mean_dz
        # get radii for spiral path
        shrink_factor = .8
        zdelta = close_depth * .2
        tt = poses[:,:3,3] # ptstocam(poses[:3,3,:].T, c2w).T
        rads = np.percentile(np.abs(tt), 90, 0)
        c2w_path = c2w
        N_views = 120
        N_rots = 2
        if path_zflat:
            # zloc = np.percentile(tt, 10, 0)[2]
            zloc = -close_depth * .1
            c2w_path[:3,3] = c2w_path[:3,3] + zloc * c2w_path[:3,2]
            rads[2] = 0.
            N_rots = 1
            N_views/=2
        # generate poses for spiral path
        render_poses = render_path_spiral(c2w_path, up, rads, focal, zdelta, zrate=.5, rots=N_rots, N=N_views)
    # transfer render poses from list to np.array
    render_poses = np.array(render_poses).astype(np.float32)

    # generate camera intrinsic K
    H, W, focal = int(poses[0, 0, 4]), int(poses[0, 1, 4]), poses[0, 2, 4]
    K = np.array([[focal, 0, 0.5*W], [0, focal, 0.5*H], [0, 0, 1]]) # camera's intrinsic matrix

    # # near, far plane for llff data
    if no_ndc: near, far = np.ndarray.min(bds) * .9, np.ndarray.max(bds) * 1.
    else: near, far = 0., 1.
    near, far = np.array(near).astype(np.float32), np.array(far).astype(np.float32)

    c2w = poses_avg(poses)
    # print('total data, poses shape: {}, imgs shape: {}, bds shape: {}'.format(poses.shape, imgs.shape, bds.shape))
    # generate training dataset or test dataset according to the split
    dists  = np.sum(np.square(c2w[:3, 3] - poses[:, :3, 3]), -1)
    i_test = np.argmin(dists)
    if not isinstance(i_test, list): i_test = [i_test]
    if llffhold > 0:
        # print('auto LLFF holdout,', llffhold)
        i_test = np.arange(imgs.shape[0])[::llffhold]
    i_val = i_test
    i_train = np.array([i for i in np.arange(int(imgs.shape[0])) if (i not in i_test and i not in i_val)])
    # get the specific data
    imgs  =  imgs[i_train] if split == 'train' else  imgs[i_test]
    poses = poses[i_train] if split == 'train' else poses[i_test]
    bds   =   bds[i_train] if split == 'train' else   bds[i_test]

    # return fake render poses if split == 'fake'
    if split == 'fake': poses = render_poses

    return imgs, poses[:, :3, :4], near, far, H, W, K



def _load_data(basedir, factor=None, width=None, height=None):
    """ Load the Original LLFF Data
    Args:
        basedir - the base directory of llff data
        factor  - the scale of the image that we needed
        width   - the width of the image we want
        height  - the height of the image we want
    """
    # load data from 'poses_bounds.npy' which includes poses + intrinsics + near + far
    poses_arr = np.load(os.path.join(basedir, 'poses_bounds.npy'))      # (N, 17)
    poses = poses_arr[:, :-2].reshape([-1, 3, 5]).transpose([1, 2, 0])  # (3, 5, N), poses + intrinsics
    bds = poses_arr[:, -2:].transpose([1, 0])                           # (2, N), near + far
    
    # load one image just for its height and width for factoring
    img0 = [os.path.join(basedir, 'images', f) for f in sorted(os.listdir(os.path.join(basedir, 'images'))) if f.endswith('JPG') or f.endswith('jpg') or f.endswith('png')][0]
    sh = imageio.imread(img0).shape
    
    sfx = ''
    # factor with a provided factor
    if factor is not None:
        sfx = '_{}'.format(factor)
        _minify(basedir, factors=[factor])
        factor = factor
    # factor according to the provided height
    elif height is not None:
        factor = sh[0] / float(height)
        width = int(sh[1] / factor)
        _minify(basedir, resolutions=[[height, width]])
        sfx = '_{}x{}'.format(width, height)
    # factor according to the provided width
    elif width is not None:
        factor = sh[1] / float(width)
        height = int(sh[0] / factor)
        _minify(basedir, resolutions=[[height, width]])
        sfx = '_{}x{}'.format(width, height)
    # no factor, original images directly
    else:
        factor = 1
    
    # check whether the directory has been created
    imgdir = os.path.join(basedir, 'images' + sfx)
    if not os.path.exists(imgdir):
        print(imgdir, 'does not exist, returning' )
        return
    
    # check whether the number of images matches the number of poses
    imgfiles = [os.path.join(imgdir, f) for f in sorted(os.listdir(imgdir)) if f.endswith('JPG') or f.endswith('jpg') or f.endswith('png')]
    if poses.shape[-1] != len(imgfiles):
        print('mismatch between imgs {} and poses {} !!!!'.format(len(imgfiles), poses.shape[-1]) )
        return
    
    # adjust the instrinsic if we factor the images
    sh = imageio.imread(imgfiles[0]).shape
    poses[:2, 4, :] = np.array(sh[:2]).reshape([2, 1])  # change the intrinsic cx, cy with new images' height and width
    poses[ 2, 4, :] = poses[ 2, 4, :] * 1./factor       # reduce the focal length of the intrinsic
    
    # read all the factorized images
    def imread(f):
        if f.endswith('png'):
            return imageio.imread(f, ignoregamma=True)
        else:
            return imageio.imread(f)
    imgs = imgs = [imread(f)[..., :3]/255. for f in imgfiles]   # [(H, W, 3), (H, W, 3), ...]
    imgs = np.stack(imgs, -1)                                   # (H, W, 3, N)
    print('loaded image data: ', imgs.shape, poses[:, -1, 0])   # 打印第一张图片的信息

    return poses, bds, imgs


def _minify(basedir, factors=[], resolutions=[]):
    needtoload = False
    for r in factors:
        imgdir = os.path.join(basedir, 'images_{}'.format(r))
        if not os.path.exists(imgdir):
            needtoload = True
    for r in resolutions:
        imgdir = os.path.join(basedir, 'images_{}x{}'.format(r[1], r[0]))
        if not os.path.exists(imgdir):
            needtoload = True
    if not needtoload:
        return
    
    from shutil import copy
    from subprocess import check_output
    
    imgdir = os.path.join(basedir, 'images')
    imgs = [os.path.join(imgdir, f) for f in sorted(os.listdir(imgdir))]
    imgs = [f for f in imgs if any([f.endswith(ex) for ex in ['JPG', 'jpg', 'png', 'jpeg', 'PNG']])]
    imgdir_orig = imgdir
    
    wd = os.getcwd()

    for r in factors + resolutions:
        if isinstance(r, int):
            name = 'images_{}'.format(r)
            resizearg = '{}%'.format(100./r)
        else:
            name = 'images_{}x{}'.format(r[1], r[0])
            resizearg = '{}x{}'.format(r[1], r[0])
        imgdir = os.path.join(basedir, name)
        if os.path.exists(imgdir):
            continue
            
        print('Minifying', r, basedir)
        
        os.makedirs(imgdir)
        check_output('cp {}/* {}'.format(imgdir_orig, imgdir), shell=True)
        
        ext = imgs[0].split('.')[-1]
        args = ' '.join(['mogrify', '-resize', resizearg, '-format', 'png', '*.{}'.format(ext)])
        print(args)
        os.chdir(imgdir)
        check_output(args, shell=True)
        os.chdir(wd)
        
        if ext != 'png':
            check_output('rm {}/*.{}'.format(imgdir, ext), shell=True)
            print('Removed duplicates')
        print('Done')


def normalize(x):
    return x / np.linalg.norm(x)

def viewmatrix(z, up, pos):
    vec2 = normalize(z)
    vec1_avg = up
    vec0 = normalize(np.cross(vec1_avg, vec2))
    vec1 = normalize(np.cross(vec2, vec0))
    m = np.stack([vec0, vec1, vec2, pos], 1)
    return m

def ptstocam(pts, c2w):
    tt = np.matmul(c2w[:3,:3].T, (pts-c2w[:3,3])[...,np.newaxis])[...,0]
    return tt

def poses_avg(poses):
    hwf = poses[0, :3, -1:]                     # (N, 3, 1), N 张照片的 camera intrinsics
    center = poses[:, :3, 3].mean(0)            # (3, ), N 个 camera 的 camera origin 平均值
    vec2 = normalize(poses[:, :3, 2].sum(0))    # (3, ), N 个 camera 的 gaze 方向平均值
    up = poses[:, :3, 1].sum(0)                 # (3, ), N 个 camera 的 up 方向平均值
    c2w = np.concatenate([viewmatrix(vec2, up, center), hwf], 1)

    return c2w



def render_path_spiral(c2w, up, rads, focal, zdelta, zrate, rots, N):
    render_poses = []
    rads = np.array(list(rads) + [1.])
    hwf = c2w[:,4:5]
    
    for theta in np.linspace(0., 2. * np.pi * rots, N+1)[:-1]:
        c = np.dot(c2w[:3,:4], np.array([np.cos(theta), -np.sin(theta), -np.sin(theta*zrate), 1.]) * rads) 
        z = normalize(c - np.dot(c2w[:3,:4], np.array([0,0,-focal, 1.])))
        render_poses.append(np.concatenate([viewmatrix(z, up, c), hwf], 1))
    return render_poses
    


def recenter_poses(poses):
    poses_ = poses+0
    bottom = np.reshape([0,0,0,1.], [1,4])
    c2w = poses_avg(poses)
    c2w = np.concatenate([c2w[:3,:4], bottom], -2)
    bottom = np.tile(np.reshape(bottom, [1,1,4]), [poses.shape[0],1,1])
    poses = np.concatenate([poses[:,:3,:4], bottom], -2)

    poses = np.linalg.inv(c2w) @ poses
    poses_[:,:3,:4] = poses[:,:3,:4]
    poses = poses_
    return poses



def spherify_poses(poses, bds):
    
    p34_to_44 = lambda p : np.concatenate([p, np.tile(np.reshape(np.eye(4)[-1,:], [1,1,4]), [p.shape[0], 1,1])], 1)
    
    rays_d = poses[:,:3,2:3]
    rays_o = poses[:,:3,3:4]

    def min_line_dist(rays_o, rays_d):
        A_i = np.eye(3) - rays_d * np.transpose(rays_d, [0,2,1])
        b_i = -A_i @ rays_o
        pt_mindist = np.squeeze(-np.linalg.inv((np.transpose(A_i, [0,2,1]) @ A_i).mean(0)) @ (b_i).mean(0))
        return pt_mindist

    pt_mindist = min_line_dist(rays_o, rays_d)
    
    center = pt_mindist
    up = (poses[:,:3,3] - center).mean(0)

    vec0 = normalize(up)
    vec1 = normalize(np.cross([.1,.2,.3], vec0))
    vec2 = normalize(np.cross(vec0, vec1))
    pos = center
    c2w = np.stack([vec1, vec2, vec0, pos], 1)

    poses_reset = np.linalg.inv(p34_to_44(c2w[None])) @ p34_to_44(poses[:,:3,:4])

    rad = np.sqrt(np.mean(np.sum(np.square(poses_reset[:,:3,3]), -1)))
    
    sc = 1./rad
    poses_reset[:,:3,3] *= sc
    bds *= sc
    rad *= sc
    
    centroid = np.mean(poses_reset[:,:3,3], 0)
    zh = centroid[2]
    radcircle = np.sqrt(rad**2-zh**2)
    new_poses = []
    
    for th in np.linspace(0.,2.*np.pi, 120):

        camorigin = np.array([radcircle * np.cos(th), radcircle * np.sin(th), zh])
        up = np.array([0,0,-1.])

        vec2 = normalize(camorigin)
        vec0 = normalize(np.cross(vec2, up))
        vec1 = normalize(np.cross(vec2, vec0))
        pos = camorigin
        p = np.stack([vec0, vec1, vec2, pos], 1)

        new_poses.append(p)

    new_poses = np.stack(new_poses, 0)
    
    new_poses = np.concatenate([new_poses, np.broadcast_to(poses[0,:3,-1:], new_poses[:,:3,-1:].shape)], -1)
    poses_reset = np.concatenate([poses_reset[:,:3,:4], np.broadcast_to(poses[0,:3,-1:], poses_reset[:,:3,-1:].shape)], -1)
    
    return poses_reset, new_poses, bds

