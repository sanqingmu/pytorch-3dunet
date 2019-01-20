import torch
import torch.nn.functional as F
from torch import nn as nn
from torch.autograd import Variable

SUPPORTED_LOSSES = ['ce', 'bce', 'wce', 'pce', 'dice', 'gdl']


def compute_per_channel_dice(input, target, out_channels, epsilon=1e-5, ignore_index=None, weight=None,
                             ignore_channel=None):
    # assumes that input is a normalized probability

    # given ignore_channel increase the number of out_channels by 1 for computation of the one-hot tensor
    if ignore_channel is not None:
        out_channels = out_channels + 1

    # input and target shapes must match
    if target.dim() == 4:
        target = expand_as_one_hot(target, C=out_channels, ignore_index=ignore_index)

    if ignore_channel is not None:
        if ignore_channel == 0:
            target = target[:, 1:, ...]
        elif ignore_channel == out_channels - 1:
            target = target[:, :-1, ...]
        else:
            target = torch.cat((target[:, 0:ignore_channel, ...], target[:, ignore_channel + 1:, ...]), dim=1)

    assert input.size() == target.size(), "'input' and 'target' must have the same shape"

    # mask ignore_index if present
    if ignore_index is not None:
        mask = target.clone().ne_(ignore_index)
        mask.requires_grad = False

        input = input * mask
        target = target * mask

    input = flatten(input)
    target = flatten(target)

    target = target.float()
    # Compute per channel Dice Coefficient
    intersect = (input * target).sum(-1)
    if weight is not None:
        intersect = weight * intersect

    denominator = (input + target).sum(-1)
    return 2. * intersect / denominator.clamp(min=epsilon)


class DiceCoefficient:
    """Computes Dice Coefficient.
    Generalized to multiple channels by computing per-channel Dice Score
    (as described in https://arxiv.org/pdf/1707.03237.pdf) and then simply taking the average.
    Input is expected to be probabilities instead of logits.
    """

    def __init__(self, out_channels, epsilon=1e-5, ignore_index=None, ignore_channel=None):
        self.out_channels = out_channels
        self.epsilon = epsilon
        self.ignore_index = ignore_index
        self.ignore_channel = ignore_channel

    def __call__(self, input, target):
        # Average across channels in order to get the final score
        return torch.mean(compute_per_channel_dice(input, target, self.out_channels, epsilon=self.epsilon,
                                                   ignore_index=self.ignore_index, ignore_channel=self.ignore_channel))


class DiceLoss(nn.Module):
    """Computes Dice Loss, which just 1 - DiceCoefficient described above.
    Additionally allows per-class weights to be provided.
    """

    def __init__(self, out_channels, epsilon=1e-5, weight=None, ignore_index=None, ignore_channel=None,
                 sigmoid_normalization=True):
        super(DiceLoss, self).__init__()
        self.out_channels = out_channels
        self.epsilon = epsilon
        self.register_buffer('weight', weight)
        self.ignore_index = ignore_index
        self.ignore_channel = ignore_channel
        # The output from the network during training is assumed to be un-normalized probabilities and we would
        # like to normalize the logits. Since Dice (or soft Dice in this case) is usually used for binary data,
        # normalizing the channels with Sigmoid is the default choice even for multi-class segmentation problems.
        # However if one would like to apply Softmax in order to get the proper probability distribution from the
        # output, just specify sigmoid_normalization=False.
        if sigmoid_normalization:
            self.normalization = nn.Sigmoid()
        else:
            self.normalization = nn.Softmax(dim=1)

    def forward(self, input, target):
        # get probabilities from logits
        input = self.normalization(input)
        if self.weight is not None:
            weight = Variable(self.weight, requires_grad=False)
        else:
            weight = None

        per_channel_dice = compute_per_channel_dice(input, target, out_channels=self.out_channels, epsilon=self.epsilon,
                                                    ignore_index=self.ignore_index, ignore_channel=self.ignore_channel,
                                                    weight=weight)
        # Average the Dice score across all channels/classes
        return torch.mean(1. - per_channel_dice)


class GeneralizedDiceLoss(nn.Module):
    """Computes Generalized Dice Loss (GDL) as described in https://arxiv.org/pdf/1707.03237.pdf
    """

    def __init__(self, out_channels, epsilon=1e-5, weight=None, ignore_index=None, ignore_channel=None,
                 sigmoid_normalization=True):
        super(GeneralizedDiceLoss, self).__init__()
        self.out_channels = out_channels
        self.epsilon = epsilon
        self.register_buffer('weight', weight)
        self.ignore_index = ignore_index
        self.ignore_channel = ignore_channel
        if sigmoid_normalization:
            self.normalization = nn.Sigmoid()
        else:
            self.normalization = nn.Softmax(dim=1)

    def forward(self, input, target):
        # get probabilities from logits
        input = self.normalization(input)
        # input and target shapes must match
        if target.dim() == 4:
            target = expand_as_one_hot(target, C=self.out_channels, ignore_index=self.ignore_index)

        if self.ignore_channel is not None:
            if self.ignore_channel == 0:
                target = target[:, 1:, ...]
            elif self.ignore_channel == self.out_channels - 1:
                target = target[:, :-1, ...]
            else:
                target = torch.cat((target[:, 0:self.ignore_channel, ...], target[:, self.ignore_channel + 1:, ...]),
                                   dim=1)

        assert input.size() == target.size(), "'input' and 'target' must have the same shape"

        # mask ignore_index if present
        if self.ignore_index is not None:
            mask = target.clone().ne_(self.ignore_index)
            mask.requires_grad = False

            input = input * mask
            target = target * mask

        input = flatten(input)
        target = flatten(target)

        target = target.float()
        target_sum = target.sum(-1)
        class_weights = Variable(1. / (target_sum * target_sum).clamp(min=self.epsilon), requires_grad=False)

        intersect = (input * target).sum(-1) * class_weights
        if self.weight is not None:
            weight = Variable(self.weight, requires_grad=False)
            intersect = weight * intersect

        denominator = (input + target).sum(-1) * class_weights

        return torch.mean(1. - 2. * intersect / denominator.clamp(min=self.epsilon))


class WeightedCrossEntropyLoss(nn.Module):
    """WeightedCrossEntropyLoss (WCE) as described in https://arxiv.org/pdf/1707.03237.pdf
    """

    def __init__(self, weight=None, ignore_index=-1):
        super(WeightedCrossEntropyLoss, self).__init__()
        self.register_buffer('weight', weight)
        self.ignore_index = ignore_index

    def forward(self, input, target):
        class_weights = self._class_weights(input)
        if self.weight is not None:
            weight = Variable(self.weight, requires_grad=False)
            class_weights = class_weights * weight
        return F.cross_entropy(input, target, weight=class_weights, ignore_index=self.ignore_index)

    @staticmethod
    def _class_weights(input):
        # normalize the input first
        input = F.softmax(input, _stacklevel=5)
        flattened = flatten(input)
        nominator = (1. - flattened).sum(-1)
        denominator = flattened.sum(-1)
        class_weights = Variable(nominator / denominator, requires_grad=False)
        return class_weights


class IgnoreIndexLossWrapper:
    """
    Wrapper around loss functions which do not support 'ignore_index', e.g. BCELoss.
    Throws exception if the wrapped loss supports the 'ignore_index' option.
    """

    def __init__(self, loss_criterion, ignore_index=-1):
        if hasattr(loss_criterion, 'ignore_index'):
            raise RuntimeError(f"Cannot wrap {type(loss_criterion)}. Use 'ignore_index' attribute instead")
        self.loss_criterion = loss_criterion
        self.ignore_index = ignore_index

    def __call__(self, input, target):
        # always expand target tensor, so that input.size() == target.size()
        if target.dim() == 4:
            target = expand_as_one_hot(target, C=input.size()[1], ignore_index=self.ignore_index)

        assert input.size() == target.size()

        mask = target.clone().ne_(self.ignore_index)
        mask.requires_grad = False

        masked_input = input * mask
        masked_target = target * mask
        return self.loss_criterion(masked_input, masked_target)


class PixelWiseCrossEntropyLoss(nn.Module):
    def __init__(self, class_weights=None, ignore_index=None):
        super(PixelWiseCrossEntropyLoss, self).__init__()
        self.register_buffer('class_weights', class_weights)
        self.ignore_index = ignore_index
        self.log_softmax = nn.LogSoftmax(dim=1)

    def forward(self, input, target, weights):
        assert target.size() == weights.size()
        # normalize the input
        log_probabilities = self.log_softmax(input)
        # standard CrossEntropyLoss requires the target to be (NxDxHxW), so we need to expand it to (NxCxDxHxW)
        target = expand_as_one_hot(target, C=input.size()[1], ignore_index=self.ignore_index)
        # expand weights
        weights = weights.unsqueeze(0)
        weights = weights.expand_as(input)

        # mask ignore_index if present
        if self.ignore_index is not None:
            mask = Variable(target.data.ne(self.ignore_index).float(), requires_grad=False)
            log_probabilities = log_probabilities * mask
            target = target * mask

        # apply class weights
        if self.class_weights is None:
            class_weights = torch.ones(input.size()[1]).float().to(input.device)
        else:
            class_weights = self.class_weights
        class_weights = class_weights.view(1, input.size()[1], 1, 1, 1)
        class_weights = Variable(class_weights, requires_grad=False)
        # add class_weights to each channel
        weights = class_weights + weights

        # compute the losses
        result = -weights * target * log_probabilities
        # average the losses
        return result.mean()


def flatten(tensor):
    """Flattens a given tensor such that the channel axis is first.
    The shapes are transformed as follows:
       (N, C, D, H, W) -> (C, N * D * H * W)
    """
    C = tensor.size(1)
    # new axis order
    axis_order = (1, 0) + tuple(range(2, tensor.dim()))
    # Transpose: (N, C, D, H, W) -> (C, N, D, H, W)
    transposed = tensor.permute(axis_order)
    # Flatten: (C, N, D, H, W) -> (C, N * D * H * W)
    return transposed.view(C, -1)


def expand_as_one_hot(input, C, ignore_index=None):
    """
    Converts NxDxHxW label image to NxCxDxHxW, where each label is stored in a separate channel
    :param input: 4D input image (NxDxHxW)
    :param C: number of channels/labels
    :param ignore_index: ignore index to be kept during the expansion
    :return: 5D output image (NxCxDxHxW)
    """
    assert input.dim() == 4

    shape = input.size()
    shape = list(shape)
    shape.insert(1, C)
    shape = tuple(shape)

    # expand the input tensor to Nx1xDxHxW
    src = input.unsqueeze(0)

    if ignore_index is not None:
        # create ignore_index mask for the result
        expanded_src = src.expand(shape)
        mask = expanded_src == ignore_index
        # clone the src tensor and zero out ignore_index in the input
        src = src.clone()
        src[src == ignore_index] = 0
        # scatter to get the one-hot tensor
        result = torch.zeros(shape).to(input.device).scatter_(1, src, 1)
        # bring back the ignore_index in the result
        result[mask] = ignore_index
        return result
    else:
        # scatter to get the one-hot tensor
        return torch.zeros(shape).to(input.device).scatter_(1, src, 1)


def get_loss_criterion(loss_str, out_channels, weight=None, ignore_index=None, ignore_channel=None):
    """
    Returns the loss function based on the loss_str.
    :param loss_str: specifies the loss function to be used
    :param out_channels: number of channels in the network output
    :param weight: a manual rescaling weight given to each class
    :param ignore_index: specifies a target value that is ignored and does not contribute to the input gradient
    :param ignore_channel: channel in the target to be ignored during training (used only with Dice losses)
    :return: an instance of the loss function
    """
    assert loss_str in SUPPORTED_LOSSES, f'Invalid loss string: {loss_str}'

    if ignore_channel is not None:
        assert loss_str in ['dice', 'gdl']

    if loss_str == 'bce':
        if ignore_index is None:
            return nn.BCEWithLogitsLoss()
        else:
            return IgnoreIndexLossWrapper(nn.BCEWithLogitsLoss(), ignore_index=ignore_index)
    elif loss_str == 'ce':
        if ignore_index is None:
            ignore_index = -100  # use the default 'ignore_index' as defined in the CrossEntropyLoss
        return nn.CrossEntropyLoss(weight=weight, ignore_index=ignore_index)
    elif loss_str == 'wce':
        if ignore_index is None:
            ignore_index = -100  # use the default 'ignore_index' as defined in the CrossEntropyLoss
        return WeightedCrossEntropyLoss(weight=weight, ignore_index=ignore_index)
    elif loss_str == 'pce':
        return PixelWiseCrossEntropyLoss(class_weights=weight, ignore_index=ignore_index)
    elif loss_str == 'gdl':
        return GeneralizedDiceLoss(out_channels=out_channels, weight=weight, ignore_index=ignore_index,
                                   ignore_channel=ignore_channel)
    else:
        return DiceLoss(out_channels=out_channels, weight=weight, ignore_index=ignore_index,
                        ignore_channel=ignore_channel)
